#!/usr/bin/env python3
"""
session_server.py - Persistent ONNX Runtime session server (separate process).

Start this process ONCE before running generate.py:
    python session_server.py

Architecture
------------
All three ONNX sessions (text_encoder, unet, vae) are created at server
startup and kept alive in memory. By the time a client connects, every
session is already loaded and the QNN HTP hardware is warm.

The client calls store.run_session(name, feeds) -- the server looks up the
pre-loaded session by name, runs inference, and returns the output arrays.
No session is ever created in the client process.

QNN execution provider (onnxruntime-qnn 2.4.0)
----------------------------------------------
As of onnxruntime-qnn 2.x the QNN EP ships as a *plugin* execution provider:
the `onnxruntime_qnn` package no longer bundles its own `onnxruntime` build
(it depends on the stock core wheel instead), and its EP library must be
registered with ORT explicitly before a session can use it. So rather than
1.x's `providers=["QNNExecutionProvider"]` + `provider_options=[...]`
arguments, session creation here is:

    ort.register_execution_provider_library(EP_NAME, qnn_ep.get_library_path())
    sess_options.add_provider_for_devices(<QNN NPU devices>, QNN_PROVIDER_OPTIONS)
    ort.InferenceSession(path, sess_options=sess_options)   # no providers= arg

This also retires the old `qnn_libs/` staging directory. The context binaries
in --model_dir were compiled with QAIRT 2.45.0; onnxruntime-qnn 2.4.0 bundles
QAIRT 2.48.40, which loads them fine (the 1.24.1 wheel bundled QAIRT 2.42 and
failed with "Using newer context binary on old SDK", error 0x1388). The DLLs
now come from the installed package, and their location differs per wheel --
the win_arm64 wheel ships them flat in `onnxruntime_qnn/`, while the win_amd64
wheel ships `onnxruntime_qnn/libs/{amd64,arm64ec}/` and picks the subdirectory
matching the host process at import time (`arm64ec` for an x64 Python emulated
on an ARM64 host, which is what this checkout's venv is). Hence the paths below
always come from `qnn_ep.get_library_path()` / `get_qnn_htp_path()` rather than
being spelled out, and no manual PATH juggling is needed.

Exposed methods (via multiprocessing.managers)
----------------------------------------------
  run_session(name, feeds)   -> list[np.ndarray]
  get_input_names(name)      -> list[str]
  get_output_names(name)     -> list[str]
"""
import argparse
import os
import sys
from multiprocessing.managers import BaseManager

import onnxruntime as ort

# Importing this also prepends the arch-appropriate QAIRT DLL directory to the
# process DLL search path, so QnnHtp.dll and friends resolve.
import onnxruntime_qnn as qnn_ep

from quantization import MODEL_DIR
from server_config import SERVER_HOST, SERVER_PORT, SERVER_AUTHKEY

# Maps logical session name -> ONNX filename (relative to --model_dir)
MODELS = {
    "text_encoder": "text_encoder.onnx",
    "unet": "unet.onnx",
    "vae": "vae.onnx",
}

EP_NAME = qnn_ep.get_ep_name()  # "QNNExecutionProvider"

QNN_PROVIDER_OPTIONS = {
    "backend_path": qnn_ep.get_qnn_htp_path(),
    "htp_graph_finalization_optimization_mode": "3",
    "htp_performance_mode": "burst",
    "vtcm_mb": "8",
}

# Server-side session registry -- populated at startup, shared across all
# SessionStore instances.
_sessions: dict = {}


def register_qnn_ep() -> list:
    """Register the onnxruntime-qnn plugin EP; return its NPU OrtEpDevice list.

    The plugin advertises one device per QNN backend it can reach (NPU, GPU and
    CPU). Only the NPU/HTP one can run this package's HTP context binaries, so
    the GPU and CPU entries are filtered out -- add_provider_for_devices()
    would otherwise let ORT place work on a backend that can't load them.
    """
    ort.register_execution_provider_library(EP_NAME, qnn_ep.get_library_path())
    devices = [
        d
        for d in ort.get_ep_devices()
        if d.ep_name == EP_NAME and d.device.type == ort.OrtHardwareDeviceType.NPU
    ]
    if not devices:
        advertised = sorted({f"{d.ep_name}/{d.device.type.name}" for d in ort.get_ep_devices()})
        raise RuntimeError(
            f"onnxruntime-qnn {qnn_ep.__version__} registered no NPU device "
            f"(advertised: {advertised or 'none'}). Check that the QAIRT DLLs match the host "
            f"process architecture and that the Snapdragon NPU driver is present."
        )
    return devices


class SessionStore:
    """Exposed to clients via multiprocessing.managers.

    Sessions are pre-loaded at server startup -- this class only provides
    access to them.
    """

    def run_session(self, name: str, feeds: dict) -> list:
        if name not in _sessions:
            raise KeyError(f"[server] Session '{name}' is not loaded. Available: {list(_sessions.keys())}")
        print(f"[server] run_session '{name}'", flush=True)
        return _sessions[name].run(None, feeds)

    def get_input_names(self, name: str) -> list:
        if name not in _sessions:
            raise KeyError(f"[server] Session '{name}' is not loaded. Available: {list(_sessions.keys())}")
        return [inp.name for inp in _sessions[name].get_inputs()]

    def get_output_names(self, name: str) -> list:
        if name not in _sessions:
            raise KeyError(f"[server] Session '{name}' is not loaded. Available: {list(_sessions.keys())}")
        return [out.name for out in _sessions[name].get_outputs()]


class SessionManager(BaseManager):
    pass


SessionManager.register("SessionStore", SessionStore)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Persistent ORT session server for SD2.1")
    parser.add_argument(
        "--model_dir",
        default=MODEL_DIR,
        help="Directory containing text_encoder.onnx / unet.onnx / vae.onnx and their "
             "*_qairt_context.bin siblings (default: the precompiled model package).",
    )
    args = parser.parse_args()

    print(f"[server] onnxruntime {ort.__version__} + onnxruntime-qnn {qnn_ep.__version__}", flush=True)
    qnn_devices = register_qnn_ep()
    print(f"[server] Registered '{EP_NAME}' from {qnn_ep.get_library_path()}", flush=True)
    print(f"[server] Using {len(qnn_devices)} QNN NPU device(s), backend {QNN_PROVIDER_OPTIONS['backend_path']}", flush=True)

    print(f"[server] Loading sessions from: {args.model_dir}", flush=True)
    for name, filename in MODELS.items():
        path = os.path.join(args.model_dir, filename)
        print(f"[server] Loading '{name}' from {path} ...", flush=True)
        so = ort.SessionOptions()
        so.add_provider_for_devices(qnn_devices, QNN_PROVIDER_OPTIONS)
        _sessions[name] = ort.InferenceSession(path, sess_options=so)
        print(f"[server] '{name}' ready (providers: {_sessions[name].get_providers()}).", flush=True)

    print(f"[server] All {len(_sessions)} sessions loaded.", flush=True)

    manager = SessionManager(address=(SERVER_HOST, SERVER_PORT), authkey=SERVER_AUTHKEY)
    server = manager.get_server()
    print(f"[server] Listening on {SERVER_HOST}:{SERVER_PORT}", flush=True)
    print("[server] Press Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] Shutting down.", flush=True)
        sys.exit(0)
