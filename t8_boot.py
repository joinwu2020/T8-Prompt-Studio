"""T8 節點脫離 ComfyUI 的啟動層：ComfyUI 替身 + 載入五個核心模組。

只要放在 comfyui-minimax-h3-prompt-enhancer-T8 節點資料夾內即可使用。
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

NODE_DIR = Path(__file__).resolve().parent


# ── comfy_api.latest.io 替身（與節點作者單元測試相同的做法，能保留 NodeOutput 的輸出值）──
class _Descriptor:
    def __init__(self, identifier=None, display_name=None, **kwargs):
        self.id = identifier
        self.display_name = display_name
        self.kwargs = kwargs


class _TypeFactory:
    @classmethod
    def Input(cls, identifier=None, display_name=None, **kwargs):
        return _Descriptor(identifier, display_name, **kwargs)

    @classmethod
    def Output(cls, display_name=None, **kwargs):
        return _Descriptor(None, display_name, **kwargs)


def _io_type(name):
    return type(name, (), {"Input": _TypeFactory.Input, "Output": _TypeFactory.Output})


class _NodeOutput(tuple):
    def __new__(cls, *values, ui=None, **kwargs):
        obj = tuple.__new__(cls, values)
        obj.ui = ui or {}
        return obj


class _IOMeta(type):
    """未列出的 io.X 型別一律回傳通用型別，節點新增型別時不會在匯入階段失敗"""

    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        t = _io_type(name)
        setattr(cls, name, t)
        return t


class _IO(metaclass=_IOMeta):
    ComfyNode = object
    NodeOutput = _NodeOutput
    Schema = staticmethod(lambda **kwargs: types.SimpleNamespace(**kwargs))
    Custom = staticmethod(lambda *_a, **_k: _io_type("CustomType"))
    Autogrow = type("Autogrow", (), {
        "Input": _TypeFactory.Input,
        "TemplatePrefix": staticmethod(lambda **kwargs: kwargs),
        "TemplateNames": staticmethod(lambda **kwargs: kwargs),
    })
    Hidden = types.SimpleNamespace(unique_id="unique_id", prompt="prompt", extra_pnginfo="extra_pnginfo")


def _install_comfy_stubs(models_dir: Path) -> None:
    comfy_api = types.ModuleType("comfy_api")
    latest = types.ModuleType("comfy_api.latest")
    latest.io = _IO
    latest.ComfyExtension = object
    latest.VideoFromFile = None
    comfy_api.latest = latest
    sys.modules.setdefault("comfy_api", comfy_api)
    sys.modules.setdefault("comfy_api.latest", latest)

    # comfy.model_management：本地 GGUF 模式會呼叫，這裡沒有 ComfyUI 模型要讓出顯存
    comfy = types.ModuleType("comfy")
    mm = types.ModuleType("comfy.model_management")
    mm.throw_exception_if_processing_interrupted = lambda: None
    mm.get_free_memory = lambda *a, **k: 1 << 40
    mm.unload_all_models = lambda *a, **k: None
    mm.soft_empty_cache = lambda *a, **k: None
    mm.interrupt_processing = False
    comfy.model_management = mm
    sys.modules.setdefault("comfy", comfy)
    sys.modules.setdefault("comfy.model_management", mm)

    # folder_paths：本地 GGUF 模型目錄（預設 ComfyUI/models/LLM）
    fp = types.ModuleType("folder_paths")
    fp.models_dir = str(models_dir)
    fp.folder_names_and_paths = {}
    fp.supported_pt_extensions = {".gguf", ".safetensors"}

    def get_folder_paths(name):
        entry = fp.folder_names_and_paths.get(name)
        return list(entry[0]) if entry else [str(Path(fp.models_dir) / name)]

    fp.get_folder_paths = get_folder_paths
    fp.get_filename_list = lambda name: sorted(
        p.name for d in get_folder_paths(name) if Path(d).is_dir() for p in Path(d).iterdir() if p.is_file())
    fp.get_full_path = lambda name, fn: next(
        (str(Path(d) / fn) for d in get_folder_paths(name) if (Path(d) / fn).is_file()), None)
    fp.get_output_directory = lambda: str(NODE_DIR / "output")
    fp.get_temp_directory = lambda: str(NODE_DIR / "temp")
    fp.get_user_directory = lambda: str(NODE_DIR / "user")
    sys.modules.setdefault("folder_paths", fp)

    # server.PromptServer：路由註冊會自動略過
    server = types.ModuleType("server")
    server.PromptServer = types.SimpleNamespace(instance=None)
    sys.modules.setdefault("server", server)


def default_models_dir() -> Path:
    env = os.environ.get("T8_WEBUI_MODELS_DIR")
    if env:
        return Path(env).expanduser()
    comfy_models = NODE_DIR.parent.parent / "models"  # ComfyUI/custom_nodes/<node>/ → ComfyUI/models
    return comfy_models if comfy_models.is_dir() else NODE_DIR / "models"


MODELS_DIR = default_models_dir()
_install_comfy_stubs(MODELS_DIR)
# 節點模組使用相對匯入 → 掛一個虛擬套件來載入（不執行節點的 __init__.py，不註冊 ComfyUI 節點）
import importlib  # noqa: E402

PKG = "t8node"
if PKG not in sys.modules:
    _pkg = types.ModuleType(PKG)
    _pkg.__path__ = [str(NODE_DIR)]
    _pkg.__file__ = str(NODE_DIR / "__init__.py")
    sys.modules[PKG] = _pkg


def load(name: str):
    return importlib.import_module(f"{PKG}.{name}")


h3 = load("nodes")                # MiniMax H3
m3 = load("music3")               # MiniMax Music 3
yue = load("yue2")                # YuE2
qwi = load("qwen_image21")        # Qwen Image 2.1
ct = load("case_templates")       # T8 非官方模板庫

__all__ = ["NODE_DIR", "MODELS_DIR", "PKG", "load", "h3", "m3", "yue", "qwi", "ct"]
