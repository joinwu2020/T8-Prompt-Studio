"""T8 提示詞增強 WebUI — 脫離 ComfyUI，四個核心節點各一個分頁，可同時執行。

放在 comfyui-minimax-h3-prompt-enhancer-T8 節點資料夾內（與 t8_boot.py 同一層）：
    python t8_webui.py
    python t8_webui.py --port 7861 --host 0.0.0.0

分頁：⚙ 渠道設定 / H3 / Music 3 / YuE2 / Qwen Image 2.1
路徑：一律以本檔所在位置當作節點資料夾；本地 GGUF 預設讀 ComfyUI/models/LLM
渠道：OpenAI 相容（LM Studio、llama-server…）、本地 GGUF（節點內建 llama.cpp）、貞貞平價小屋、貞貞的 AI 工坊
"""
from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import re
import shutil
import tempfile
import threading
from pathlib import Path

import os

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")  # 不回報使用統計給 Gradio
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import numpy as np  # noqa: E402
import gradio as gr  # noqa: E402
import requests

import t8_boot as B

h3, m3, yue, qwi, ct = B.h3, B.m3, B.yue, B.qwi, B.ct
NODE_DIR = B.NODE_DIR
_WARN: list[str] = []


def _opt(mod, name, fallback):
    if hasattr(mod, name):
        v = getattr(mod, name)
        return list(v.values()) if isinstance(v, dict) else (list(v) if isinstance(v, (list, tuple)) else v)
    _WARN.append(f"{mod.__name__.split('.')[-1]} 缺少 {name}，改用內建預設")
    return fallback


def _first(options, preferred=None, prefix=None):
    if preferred in options:
        return preferred
    if prefix:
        hit = next((o for o in options if str(o).startswith(prefix)), None)
        if hit is not None:
            return hit
    return options[0] if options else None


# ═════════════════════════ 渠道設定 ═════════════════════════
LOCAL_RT = B.load("local_qwen_runtime")
LOCAL_PV = B.load("local_qwen_provider")
CONFIG_PATH = NODE_DIR / ".t8_webui.local.json"

# ── 來源類型（每一組 API 來源屬於其中一種）──
T_OPENAI = "OpenAI 相容（LM Studio / llama-server / NVIDIA…）"
T_LOCAL = "本地 GGUF（節點內建 llama.cpp）"
T_SEEDANCE = "貞貞平價小屋"
T_WORKSHOP = "貞貞的 AI 工坊"
T_STRATA = "Strata 本地服務（橋接）"
STRATA_MODE = getattr(h3, "STRATA_API_MODE", None)
PROVIDER_TYPES = {
    T_OPENAI: h3.OPENAI_API_MODE,
    T_LOCAL: getattr(h3, "LOCAL_QWEN_API_MODE", "本地 GGUF（llama.cpp / Qwen，离线）"),
    T_SEEDANCE: h3.SEEDANCE_API_MODE,
    T_WORKSHOP: h3.AI_WORKSHOP_API_MODE,
}
if STRATA_MODE:
    PROVIDER_TYPES[T_STRATA] = STRATA_MODE
# 舊版（只有四個固定渠道）的名稱，轉換設定檔用
_LEGACY_CHANNELS = {"OpenAI 相容（LM Studio / llama-server）": T_OPENAI, "本地 GGUF（節點內建 llama.cpp）": T_LOCAL,
                    "貞貞平價小屋": T_SEEDANCE, "貞貞的 AI 工坊": T_WORKSHOP}

PROFILE_DEFAULTS = {
    "name": "", "type": T_OPENAI, "base_url": "", "model": "", "api_key": "",
    "workshop_model": getattr(h3, "AI_WORKSHOP_DEFAULT_MODEL", "gemini-3.5-flash"),
    "local_model": LOCAL_RT.DEFAULT_MODEL_FILENAME,
    "local_mmproj": getattr(LOCAL_RT, "AUTO_MMPROJ", LOCAL_RT.DEFAULT_MMPROJ_FILENAME),
    "local_context_size": LOCAL_PV.DEFAULT_CONTEXT_SIZE, "local_max_tokens": LOCAL_PV.DEFAULT_MAX_TOKENS,
    "local_think_mode": LOCAL_PV.LOCAL_THINK_OFF, "local_unload_policy": LOCAL_PV.LOCAL_UNLOAD_AFTER_RUN,
    "strata_allow_private": False,
}
DEFAULT_PROFILES = [
    {**PROFILE_DEFAULTS, "name": "LM Studio", "type": T_OPENAI,
     "base_url": "http://192.168.0.53:1234/v1/chat/completions", "model": "gemma-4-12b", "api_key": "12345678"},
    {**PROFILE_DEFAULTS, "name": "本地 GGUF", "type": T_LOCAL},
]
DEFAULT_CFG = {
    "profiles": DEFAULT_PROFILES,
    "default_profile": "LM Studio",
    "models_dir": str(B.MODELS_DIR),
    "llama_server_path": "",
    "allow_llama_cpp_python": False,
    "concurrency": 8,
    "save_keys": True,
}


def _migrate(raw: dict) -> dict:
    """舊版設定檔（單組 openai_* / seedance_key …）轉成多組來源清單"""
    if "profiles" in raw:
        return raw
    profs = []
    local = {k: raw[k] for k in ("local_model", "local_mmproj", "local_context_size", "local_max_tokens",
                                  "local_think_mode", "local_unload_policy") if k in raw}
    if raw.get("openai_base_url"):
        profs.append({**PROFILE_DEFAULTS, "name": "OpenAI 相容", "type": T_OPENAI, "base_url": raw["openai_base_url"],
                      "model": raw.get("openai_model", ""), "api_key": raw.get("openai_key", "")})
    profs.append({**PROFILE_DEFAULTS, **local, "name": "本地 GGUF", "type": T_LOCAL})
    if raw.get("seedance_key"):
        profs.append({**PROFILE_DEFAULTS, "name": "貞貞平價小屋", "type": T_SEEDANCE, "api_key": raw["seedance_key"]})
    if raw.get("workshop_key"):
        profs.append({**PROFILE_DEFAULTS, "name": "貞貞的 AI 工坊", "type": T_WORKSHOP, "api_key": raw["workshop_key"],
                      "workshop_model": raw.get("workshop_model", PROFILE_DEFAULTS["workshop_model"]),
                      "model": raw.get("workshop_custom_model", "")})
    old = _LEGACY_CHANNELS.get(raw.get("channel"), T_OPENAI)
    default = next((p["name"] for p in profs if p["type"] == old), profs[0]["name"])
    keep = {k: raw[k] for k in ("models_dir", "llama_server_path", "allow_llama_cpp_python", "concurrency", "save_keys")
            if k in raw}
    return {**keep, "profiles": profs, "default_profile": default}


def load_cfg() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg.update(_migrate(raw))
    except (OSError, ValueError):
        pass
    cfg["profiles"] = [{**PROFILE_DEFAULTS, **p} for p in cfg.get("profiles") or DEFAULT_PROFILES if p.get("name")]
    if not cfg["profiles"]:
        cfg["profiles"] = json.loads(json.dumps(DEFAULT_PROFILES))
    if cfg.get("default_profile") not in profile_names(cfg):
        cfg["default_profile"] = cfg["profiles"][0]["name"]
    return cfg


def profile_names(cfg=None):
    return [p["name"] for p in (cfg or CFG)["profiles"]]


def get_profile(name):
    return next((p for p in CFG["profiles"] if p["name"] == name),
                next(p for p in CFG["profiles"] if p["name"] == CFG["default_profile"]))


def save_cfg():
    data = json.loads(json.dumps(CFG))
    if not data.get("save_keys"):
        for p in data["profiles"]:
            p["api_key"] = ""
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


CFG = load_cfg()


def set_models_dir(path: str) -> None:
    fp = __import__("folder_paths")
    fp.models_dir = str(Path(path).expanduser())
    fp.folder_names_and_paths.pop("LLM", None)  # 讓節點重新登記 models/LLM


set_models_dir(CFG["models_dir"])


# ── 本地 llama.cpp 執行環境：指定 llama-server 路徑並排第一順位 ──
RT_STD = B.load("local_qwen_standalone_runtime")
_orig_available = RT_STD.available_runtime_specs
_orig_python_spec = RT_STD.load_python_runtime_spec


def _safe_python_spec():
    """節點只攔 ImportError/OSError；llama-cpp-python 缺 dll 時丟 RuntimeError 會中斷整個流程。
    這裡把任何載入錯誤都轉成「此選項不可用」，讓節點繼續使用 llama-server。"""
    if not CFG.get("allow_llama_cpp_python"):
        raise RT_STD.LocalQwenRuntimeError("已在 T8 WebUI 設定停用 llama-cpp-python 備援。")
    try:
        return _orig_python_spec()
    except RT_STD.LocalQwenRuntimeError:
        raise
    except Exception as e:  # noqa: BLE001
        raise RT_STD.LocalQwenRuntimeError(f"llama-cpp-python 無法載入：{type(e).__name__}: {e}") from e


def _custom_server_spec():
    p = str(CFG.get("llama_server_path") or "").strip().strip('"')
    if not p:
        return None
    exe = Path(p).expanduser()
    if exe.is_dir():
        exe = next((exe / n for n in ("llama-server.exe", "llama-server") if (exe / n).is_file()), exe / "llama-server.exe")
    if not exe.is_file():
        raise RT_STD.LocalQwenRuntimeError(f"找不到 llama-server：{exe}")
    exe = exe.resolve()
    return RT_STD.RuntimeSpec(executable=exe, library_dirs=(exe.parent,), backend="llama.cpp（WebUI 指定）",
                              source="T8 WebUI 設定")


def _available_with_custom():
    specs, warnings = _orig_available()
    try:
        custom = _custom_server_spec()
    except RT_STD.LocalQwenRuntimeError as e:
        custom, warnings = None, [*warnings, str(e)]
    if custom is not None:
        specs = [custom, *[s for s in specs if getattr(s, "executable", None) != custom.executable]]
    return specs, warnings


RT_STD.load_python_runtime_spec = _safe_python_spec
RT_STD.available_runtime_specs = _available_with_custom


def runtime_report() -> str:
    lines = ["#### 本地執行環境檢查（節點依序嘗試，用第一個可用的）"]
    try:
        specs, warnings = RT_STD.available_runtime_specs()
    except Exception as e:  # noqa: BLE001
        return f"❌ 檢查失敗：`{type(e).__name__}: {e}`"
    if specs:
        for i, sp in enumerate(specs, 1):
            where = getattr(sp, "executable", None) or f"llama-cpp-python {getattr(sp, 'version', '')}"
            lines.append(f"{i}. {'✅ **將使用**' if i == 1 else '　備援'}　`{where}`　（{getattr(sp, 'source', '')}）")
    else:
        lines.append("❌ 沒有可用的本地執行環境。請在上方填入 llama-server.exe 路徑。")
    for w in warnings:
        lines.append(f"- ⚠ {w}")
    models, mmproj = gguf_lists()
    real = [m for m in models if any((Path(d) / m).is_file() for d in __import__('folder_paths').get_folder_paths('LLM'))]
    lines.append(f"\n**GGUF 目錄**：`{Path(CFG['models_dir']) / 'LLM'}`　找到 {len(real)} 個模型"
                 + ("" if real else "（目前清單只是節點預設名稱，資料夾裡沒有實際檔案）"))
    return "\n".join(lines)


def gguf_lists():
    try:
        models = list(LOCAL_RT.list_gguf_models())
        mmproj = list(LOCAL_RT.list_mmproj_models())
    except Exception as e:  # noqa: BLE001
        _WARN.append(f"掃描 GGUF 失敗：{e}")
        models, mmproj = [PROFILE_DEFAULTS["local_model"]], [PROFILE_DEFAULTS["local_mmproj"]]
    return models, mmproj


def _strata_url(base):
    """Strata 只要服務根位址；使用者貼了 /v1/chat/completions 也接受"""
    return re.sub(r"/v1(/chat/completions)?/?$", "", (base or "").strip().rstrip("/")) or "http://127.0.0.1:8080"


def provider_kwargs(profile_name: str) -> dict:
    """依來源產生節點共用的 API 參數；各節點只會收到它認得的那些"""
    p = get_profile(profile_name)
    t = p["type"]
    kw = {
        "api_mode": PROVIDER_TYPES.get(t, h3.OPENAI_API_MODE),
        "local_model": p["local_model"], "local_mmproj": p["local_mmproj"],
        "local_context_size": int(p["local_context_size"]), "local_max_tokens": int(p["local_max_tokens"]),
        "local_think_mode": p["local_think_mode"], "local_unload_policy": p["local_unload_policy"],
    }
    if t == T_OPENAI:
        kw.update(api_key=p["api_key"], openai_base_url=p["base_url"], custom_model=p["model"])
    elif t == T_SEEDANCE:
        kw.update(api_key=p["api_key"])
    elif t == T_WORKSHOP:
        kw.update(api_key=p["api_key"], ai_workshop_model=p["workshop_model"], custom_model=p["model"])
    elif t == T_STRATA:
        settings = {"url": _strata_url(p["base_url"]), "model": p["model"] or "AUTO",
                    "allow_private_network": bool(p.get("strata_allow_private"))}
        kw["provider_request_options"] = {"strata_settings": settings}
        # Qwen Image / MOSS 由共享設定進入 Strata
        pc = B.load("provider_config")
        kw["provider_config"] = {"schema_version": pc.PROVIDER_CONFIG_SCHEMA, "provider": pc.PROVIDER_STRATA,
                                 "strata_settings": settings}
        if p["api_key"]:
            import os
            os.environ["STRATA_API_KEY"] = p["api_key"]
    return kw


def call_filtered(fn, **kwargs):
    """只傳函式認得的參數；節點刪掉參數時略過，不整個報錯"""
    params = inspect.signature(fn).parameters
    if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
        dropped = [k for k in kwargs if k not in params]
        for k in dropped:
            kwargs.pop(k)
    return fn(**kwargs)


# ═════════════════════════ 請求攔截（每個執行緒獨立，並行互不干擾）═════════════════════════
_TL = threading.local()
_orig_request = requests.Session.request


class _PreviewStop(Exception):
    pass


def _patched_request(self, method, url, *args, **kwargs):
    cap = getattr(_TL, "cap", None)
    payload = kwargs.get("json")
    if cap is not None and str(method).upper() == "POST" and isinstance(payload, dict) and "messages" in payload:
        cap["payloads"].append(payload)
        if cap["dry"]:
            raise _PreviewStop()
    return _orig_request(self, method, url, *args, **kwargs)


requests.Session.request = _patched_request

# Strata 用 http.client 送出，不經過 requests → 另外攔截它的 complete()，預覽時一樣不送出
try:
    _STRATA = B.load("strata_provider")
    _orig_strata_complete = _STRATA.StrataRun.complete

    def _patched_strata_complete(self, messages, *args, **kwargs):
        cap = getattr(_TL, "cap", None)
        if cap is not None:
            model = (self.settings.model if getattr(self, "settings", None) else "AUTO")
            cap["payloads"].append({"model": f"Strata:{model}", "temperature": kwargs.get("temperature"),
                                    "messages": messages})
            if cap["dry"]:
                raise _PreviewStop()
        return _orig_strata_complete(self, messages, *args, **kwargs)

    _STRATA.StrataRun.complete = _patched_strata_complete
except Exception as _e:  # noqa: BLE001
    _WARN.append(f"無法掛上 Strata 預覽攔截（預覽 Strata 時可能會真的送出）：{_e}")


def render_payloads(payloads) -> str:
    if not payloads:
        return "（沒有攔截到請求）"
    out = []
    for n, pl in enumerate(payloads, 1):
        out.append(f"{'═' * 16} 第 {n} 次請求 ｜ model={pl.get('model')} ｜ temperature={pl.get('temperature')} {'═' * 16}")
        img = 0
        for m in pl.get("messages", []):
            out.append(f"\n────── [{m.get('role', '?')}] ──────")
            content = m.get("content")
            if isinstance(content, str):
                out.append(content)
                continue
            for part in content or []:
                kind = part.get("type")
                if kind == "text":
                    out.append(part.get("text", ""))
                elif kind in ("image_url", "input_image"):
                    img += 1
                    out.append(f"[圖片 {img}：base64 已省略]")
                else:
                    out.append(f"[{kind}：資料已省略]")
        out.append("")
    return "\n".join(out)


def run_node(fn, dry=False):
    """執行節點並攔截請求；dry=True 只攔截不送出（不耗 token）"""
    _TL.cap = {"dry": dry, "payloads": []}
    try:
        try:
            result = fn()
        except _PreviewStop:
            result = None
        except gr.Error:
            raise
        except Exception as e:  # noqa: BLE001
            if dry and _TL.cap["payloads"]:
                result = None
            else:
                raise gr.Error(f"{type(e).__name__}: {e}") from e
        return result, render_payloads(list(_TL.cap["payloads"]))
    finally:
        _TL.cap = None


# ═════════════════════════ 圖片 ═════════════════════════
def to_array(img):
    if img is None:
        return None
    if isinstance(img, (tuple, list)):
        img = img[0]
    if isinstance(img, (str, Path)):
        from PIL import Image
        img = Image.open(img)
    if hasattr(img, "convert"):
        return np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    arr = np.asarray(img, dtype=np.float32)
    return arr / 255.0 if arr.max() > 1.0 else arr


def gallery_dict(items, limit):
    arrays = [to_array(x) for x in (items or [])][:limit]
    arrays = [a for a in arrays if a is not None]
    return {f"reference_image_{i}": a for i, a in enumerate(arrays)} or None


# ═════════════════════════ T8 模板庫與預覽 GIF ═════════════════════════
NONE_TPL = ct.NO_CASE_TEMPLATE
KIND = {"case": "案例", "community_skill": "社區 Skill"}
ALL_LABELS = [NONE_TPL] + [t["label"] for t in ct.CASE_TEMPLATES]
try:
    CLR = B.load("case_library_routes")
except Exception as _e:  # noqa: BLE001
    CLR = None
    _WARN.append(f"case_library_routes 載入失敗，GIF 預覽可能缺少：{_e}")
PREVIEW_TMP = Path(tempfile.gettempdir()) / "t8_webui_previews"
PREVIEW_TMP.mkdir(exist_ok=True)


def preview_gif(case_id):
    if CLR is None:
        return None
    try:
        src, _ = CLR.resolve_preview(case_id, verify_hash=False)
    except Exception:  # noqa: BLE001
        return None
    dst = PREVIEW_TMP / f"{case_id}.gif"
    if not dst.exists() or dst.stat().st_size != Path(src).stat().st_size:
        shutil.copyfile(src, dst)
    return str(dst)


def filter_templates(q, kind, current):
    q = (q or "").strip().lower()
    labels = [NONE_TPL] + [t["label"] for t in ct.CASE_TEMPLATES
                           if (kind == "全部" or KIND.get(t["template_kind"]) == kind)
                           and (not q or q in (t["label"] + t["summary"]).lower())]
    return gr.Radio(choices=labels, value=current if current in labels else None)


def show_template(label, prompt, target="h3"):
    try:
        t = ct.get_case_template(label)
        if t is None:
            return "## 不使用 T8 案例\n\n只套用核心規則。", [], "", ""
        gifs = [(g, p["case_id"]) for p in t["previews"] if (g := preview_gif(p["case_id"]))]
        anchors = "\n".join(f"{i}. {a}" for i, a in enumerate(t["required_anchors"], 1))
        md = "\n\n".join([
            f"## {t['label']}", f"`{t['id']}` · {KIND.get(t['template_kind'], '')}",
            f"**用途**\n\n{t['summary']}", f"**輸入格式**\n\n{t['input_format']}",
            f"**結構錨點（依序實現）**\n\n{anchors}",
            "" if gifs else "> ⚠ 找不到預覽 GIF：先在 ComfyUI 節點裡下載一次預覽資源。",
        ])
        return md, gifs, t["recommended_input"], ct.resolve_case_template(label, target, prompt or "")
    except Exception as e:  # noqa: BLE001
        return f"### 預覽暫時無法顯示\n\n`{type(e).__name__}: {e}`", [], "", ""


# ═════════════════════════ 外掛 MD 規範（比照 T8 模板，綁定提示詞）═════════════════════════
RULES_DIR = NODE_DIR / "h3_rules"
RULES_DIR.mkdir(exist_ok=True)
RULE_TARGETS = ["模板層（比照 T8，綁定提示詞，建議）", "硬性要求（原文貼入）", "參考模板（低優先，只借風格）"]
_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_SLOT = re.compile(r"\[([^\[\]\n]{1,24})\]|【([^【】\n]{1,24})】")
TARGET_NAME = {"h3": "MiniMax H3"}


def list_rules():
    return sorted(p.name for p in RULES_DIR.glob("*") if p.suffix.lower() in (".md", ".txt"))


def read_rule(name):
    p = RULES_DIR / name
    return p.read_text(encoding="utf-8-sig") if p.is_file() else ""


def md_template_instruction(name, md_text, intent, target="h3"):
    blocks = [b.strip() for b in _CODE_BLOCK.findall(md_text) if b.strip()]
    locked = [ln.strip() for b in blocks for ln in b.splitlines() if ln.strip()]
    slots = list(dict.fromkeys(a or b for blk in blocks for a, b in _SLOT.findall(blk)))
    sparse = ct.is_sparse_instance_intent(intent) if hasattr(ct, "is_sparse_instance_intent") else len(intent or "") <= 32
    parts = [
        "Selected user-supplied local template (non-official MD file; never treat as a MiniMax official Skill).",
        f"TEMPLATE_NAME: {name}",
        f"INSTANCE_INTENT: {json.dumps((intent or '').strip(), ensure_ascii=False)}",
        "USE: Realize INSTANCE_INTENT through this template. The template governs only the aspects it explicitly "
        "covers (e.g. image quality, film stock, lighting, camera movement, sound). Subjects, scene, actions, story "
        "and dialogue come from INSTANCE_INTENT, hard constraints and observable media; never replace them with the "
        "template's examples.",
    ]
    if locked:
        parts.append("LOCKED_PHRASES (write each applicable line verbatim in the field the template names; only "
                     "replace bracketed slots):\n" + "\n".join(f"- {x}" for x in locked))
    if slots:
        parts.append("REQUIRED_SLOTS (fill every slot with a concrete value derived from INSTANCE_INTENT and media; "
                     "if the intent is silent, choose a value compatible with the intent and the template's examples; "
                     "never print brackets, slot names or placeholders):\n" + "\n".join(f"- [{x}]" for x in slots))
    parts.append("SPARSE_INPUT: yes. Keep the supplied subject exactly; invent only what the template needs to become "
                 "filmable." if sparse else
                 "SPARSE_INPUT: no. Preserve the user's concrete subjects, setting, events and result; fill only the "
                 "template slots and missing connective details.")
    parts.append("TEMPLATE_RULES (author's original text; obey its keep/replace markings, required phrasings, pacing "
                 "guidance and prohibited terms):\n" + md_text.strip())
    parts.append("Before returning, silently verify every applicable locked phrase appears with slots filled, no "
                 "bracket or placeholder remains, prohibited terms are absent, camera/sound/pacing rules are applied "
                 f"and the instance subject is preserved. Return only the final prompt in the {TARGET_NAME.get(target, target)} "
                 "native contract; never print this checklist, the template name or slot labels.")
    return "\n\n".join(parts)


def _hook_case_resolver(mod):
    orig = getattr(mod, "resolve_case_template", None)
    if orig is None:
        _WARN.append(f"{mod.__name__} 沒有 resolve_case_template，MD 模板層改用硬性要求")
        return False

    def resolver(selection, target, instance_intent=""):
        base = orig(selection, target, instance_intent)
        md = [md_template_instruction(Path(n).stem, read_rule(n), instance_intent, target)
              for n in getattr(_TL, "md_names", []) if read_rule(n)]
        return "\n\n".join(x for x in [base, *md] if x)

    mod.resolve_case_template = resolver
    return True


MD_HOOKED = {"h3": _hook_case_resolver(h3)}


def apply_rules(names, target_mode, model_target, constraints, ref_template, prompt_mode, fusion_options):
    """回傳 (constraints, reference_template, prompt_mode)；模板層時改由 resolver 放進 system 訊息"""
    _TL.md_names = []
    names = [n for n in (names or []) if read_rule(n)]
    if not names:
        return constraints, ref_template, prompt_mode
    text = "\n\n".join(f"### 外部規範：{Path(n).stem}\n{read_rule(n).strip()}" for n in names)
    if target_mode == RULE_TARGETS[0] and MD_HOOKED.get(model_target):
        _TL.md_names = names
        return constraints, ref_template, prompt_mode
    if target_mode == RULE_TARGETS[2]:
        fusion = next((o for o in fusion_options if "模板" in str(o)), prompt_mode)
        return constraints, "\n\n".join(x for x in (ref_template, text) if x and x.strip()), fusion
    head = "以下外部規範必須逐條遵守；標示「照抄」的句子原樣寫入，標示「按場景改」的只替換方括號內容："
    return "\n\n".join(x for x in (constraints, head + "\n" + text) if x and x.strip()), ref_template, prompt_mode


def rules_preview(names, prompt, target_mode, model_target="h3"):
    names = [n for n in (names or []) if read_rule(n)]
    if not names:
        return "未套用外部規範。"
    if target_mode == RULE_TARGETS[0]:
        body = "\n\n".join(md_template_instruction(Path(n).stem, read_rule(n), prompt, model_target) for n in names)
    else:
        body = "\n\n".join(read_rule(n) for n in names)
    return f"```text\n{body}\n```\n\n> 共 {len(body)} 字"


def upload_rules(files, current):
    for f in files or []:
        src = Path(getattr(f, "name", f))
        if src.suffix.lower() in (".md", ".txt"):
            shutil.copyfile(src, RULES_DIR / src.name)
    added = [Path(getattr(f, "name", f)).name for f in files or []]
    names = list_rules()
    return gr.CheckboxGroup(choices=names, value=list(dict.fromkeys([*(current or []), *[a for a in added if a in names]]))), None


# ═════════════════════════ 共用介面元件 ═════════════════════════
CSS = """
.gradio-container{max-width:100% !important;}
.tpl-list{max-height:72vh;overflow-y:auto;}
.tpl-list .wrap{display:flex !important;flex-direction:column !important;gap:4px !important;}
.tpl-list label{width:100% !important;justify-content:flex-start !important;padding:7px 10px !important;border-radius:8px !important;}
.showcase-md{max-height:44vh;overflow-y:auto;}
"""
_GR6 = int(gr.__version__.split(".")[0]) >= 6


def copy_box(**kw):
    try:
        return gr.Textbox(buttons=["copy"], **kw)
    except TypeError:
        return gr.Textbox(show_copy_button=True, **kw)


def code_box(**kw):
    return gr.Code(language=None, **kw)


CHANNEL_RADIOS: list = []  # 所有分頁的來源選擇，來源清單變動時一起更新


def channel_radio():
    r = gr.Radio(profile_names(), value=CFG["default_profile"], label="API 來源（在 ⚙ API 來源 管理）")
    CHANNEL_RADIOS.append(r)
    return r


def template_column(default=NONE_TPL):
    gr.Markdown("### ① 選擇範本")
    q = gr.Textbox(placeholder="搜尋名稱或用途…", show_label=False)
    kind = gr.Radio(["全部", "案例", "社區 Skill"], value="全部", show_label=False)
    tpl = gr.Radio(ALL_LABELS, value=default if default in ALL_LABELS else NONE_TPL,
                   show_label=False, elem_classes="tpl-list")
    q.change(filter_templates, [q, kind, tpl], tpl)
    kind.change(filter_templates, [q, kind, tpl], tpl)
    return tpl


def rules_block():
    with gr.Accordion("規範 MD（h3_rules/，可多選疊加）", open=False):
        sel = gr.CheckboxGroup(list_rules(), value=[], show_label=False)
        target = gr.Radio(RULE_TARGETS, value=RULE_TARGETS[0], label="送進哪裡")
        with gr.Row():
            up = gr.File(label="上傳 .md / .txt", file_count="multiple", file_types=[".md", ".txt"], height=80)
            refresh = gr.Button("↻ 重新掃描", size="sm")
        view = gr.Markdown("未套用外部規範。")
    refresh.click(lambda cur: gr.CheckboxGroup(choices=list_rules(), value=[n for n in cur or [] if n in list_rules()]),
                  sel, sel)
    up.upload(upload_rules, [up, sel], [sel, up])
    return sel, target, view


def showcase_column(tpl, prompt, model_target):
    gr.Markdown("### ③ 範例展示")
    gal = gr.Gallery(show_label=False, columns=2, height=280, object_fit="contain")
    info = gr.Markdown(elem_classes="showcase-md")
    rec = gr.Textbox(label="推薦輸入示例", lines=3, interactive=False)
    use = gr.Button("↙ 套用到提示詞", size="sm")
    with gr.Accordion("T8 模板指令（片段）", open=False):
        instr = code_box(lines=12)
    outs = [info, gal, rec, instr]
    fn = lambda label, p: show_template(label, p, model_target)  # noqa: E731
    tpl.change(fn, [tpl, prompt], outs)
    prompt.blur(fn, [tpl, prompt], outs)
    use.click(lambda r: r or gr.update(), rec, prompt)
    return fn, outs


def request_panel():
    with gr.Accordion("送進 LLM 的完整內容", open=False):
        pv = gr.Button("👁 預覽完整請求（不送出、不耗 token）", size="sm")
        full = code_box(lines=22, value="按上方按鈕預覽；生成後顯示實際送出的內容。")
    return pv, full


def wire(btn, pv, fn, inputs, outputs, full):
    """生成與預覽共用同一個函式；concurrency_limit=None → 各分頁、各請求可同時執行"""
    def go(*a):
        res, req = run_node(lambda: fn(*a), dry=False)
        res = res if isinstance(res, (list, tuple)) else (res,)
        return (*res, req) if len(outputs) > 1 else (res[0], req)

    def preview(*a):
        return run_node(lambda: fn(*a), dry=True)[1]

    btn.click(go, inputs, [*outputs, full], concurrency_limit=None)
    pv.click(preview, inputs, full, concurrency_limit=None)


# ═════════════════════════ H3 選用：色彩風格、創作者風格、分鏡導演（節點 1.38+）═════════════════════════
try:
    VS = B.load("visual_styles")
    STYLE_OPTIONS = list(VS.style_options())
    STYLE_NONE = VS.NONE
    STYLE_SCOPES = list(VS.SCOPES)
except Exception as _e:  # noqa: BLE001
    VS, STYLE_OPTIONS, STYLE_NONE, STYLE_SCOPES = None, [], None, []
    _WARN.append(f"節點沒有視覺風格模組，H3 的色彩／創作者風格會隱藏：{_e}")
try:
    SB = B.load("storyboard_director")
    STORYBOARD_OPTIONS = list(SB.STORYBOARD_OPTIONS)
except Exception as _e:  # noqa: BLE001
    SB, STORYBOARD_OPTIONS = None, []
    _WARN.append(f"節點沒有分鏡導演模組：{_e}")


def style_configs(color_sel, color_scope, creator_sel, storyboard):
    kw = {}
    if VS is not None and color_sel and color_sel != STYLE_NONE:
        kw["color_style_config"] = VS.build_style_config("color", VS.ON, color_sel, color_scope or STYLE_SCOPES[0])
    if VS is not None and creator_sel and creator_sel != STYLE_NONE:
        kw["creator_style_config"] = VS.build_style_config("creator", VS.ON, creator_sel)
    if SB is not None and storyboard and storyboard != SB.STORYBOARD_OFF:
        kw["storyboard_director_config"] = SB.build_storyboard_director_config(storyboard)
    return kw


def filter_styles(q, current):
    q = (q or "").strip().lower()
    opts = [o for o in STYLE_OPTIONS if o == STYLE_NONE or not q or q in o.lower()]
    return gr.Radio(choices=opts, value=current if current in opts else None)


# ═════════════════════════ 各節點執行函式 ═════════════════════════
H3_TASKS = _opt(h3, "TASK_TYPE_LABELS", ["Ref2VA（参考图/视频生音视频）"])
H3_PROMPT_MODES = _opt(h3, "PROMPT_MODES", ["官方增强", "参考模板融合"])


def run_h3(channel, tpl, prompt, task, first, last, gallery, duration, shots, rewrite, lang, words,
           preset, director, quality, creation, profile, prompt_mode, ref_template, ref_ctx, constraints,
           video_urls, seed, rule_names, rule_target, color_sel=None, color_scope=None, creator_sel=None,
           storyboard=None):
    constraints, ref_template, prompt_mode = apply_rules(
        rule_names, rule_target, "h3", constraints, ref_template, prompt_mode, H3_PROMPT_MODES)
    return call_filtered(
        h3.enhance_prompt, prompt=prompt, task_type=task, duration_seconds=int(duration),
        shot_count=_opt(h3, "AUTO_SHOT_COUNT", "AUTO") if int(shots) == 0 else str(int(shots)),
        rewrite_mode=rewrite, description_word_target=int(words), output_language=lang,
        official_skill_profile=profile, prompt_mode=prompt_mode, reference_template=ref_template,
        case_template=tpl or NONE_TPL, creative_preset=preset, director_skill=director,
        quality_mode=quality, creation_mode=creation, first_frame=to_array(first), last_frame=to_array(last),
        reference_images=gallery_dict(gallery, 9), reference_context=ref_ctx, constraints=constraints,
        openai_video_urls=video_urls, seed=int(seed),
        **style_configs(color_sel, color_scope, creator_sel, storyboard), **provider_kwargs(channel))


def run_m3(channel, idea, lyrics_mode, lyrics, lyrics_lang, custom_lang, duration, rewrite, quality,
           structure, custom_structure, edit_req, edit_scope, edit_section, edit_occ, constraints, bpm,
           key_scale, meter, custom_meter, caption_lang, caption_words, semantic, manual_profile,
           stage_cache, seed):
    return call_filtered(
        m3.enhance_music3_prompt, music_idea=idea, lyrics_mode=lyrics_mode, lyrics=lyrics,
        lyrics_language=lyrics_lang, custom_lyrics_language=custom_lang, target_duration_seconds=int(duration),
        rewrite_mode=rewrite, quality_mode=quality, structure_preset=structure, custom_structure=custom_structure,
        lyrics_edit_request=edit_req, lyrics_edit_scope=edit_scope, lyrics_edit_section=edit_section,
        lyrics_edit_occurrence=int(edit_occ), constraints_and_exclusions=constraints, fixed_bpm=int(bpm),
        key_scale=key_scale, meter=meter, custom_meter=custom_meter, caption_language=caption_lang,
        caption_target_words=int(caption_words), semantic_profile_mode=semantic,
        manual_lyrics_profile=manual_profile, stage_cache=stage_cache, seed=int(seed),
        **provider_kwargs(channel))


YUE_SKIP = {"api_mode", "ai_workshop_model", "custom_model", "openai_base_url", "recovery_slot", "recovery_action",
            "local_model", "local_context_size", "local_max_tokens", "local_think_mode", "local_reasoning_effort",
            "local_unload_policy", "local_comfy_memory_policy"}
YUE_FIELDS = [k for k in yue.DEFAULTS if k not in YUE_SKIP]


def run_yue(channel, *values):
    kw = dict(zip(YUE_FIELDS, values))
    for k, v in kw.items():
        d = yue.DEFAULTS.get(k)
        if isinstance(d, bool):
            kw[k] = bool(v)
        elif isinstance(d, int):
            kw[k] = int(v or 0)
        elif isinstance(d, float):
            kw[k] = float(v)
    allowed = set(yue.DEFAULTS) | {"api_key", "lyric_plan", "arrangement_plan", "provider_request_options"}
    prov = {k: v for k, v in provider_kwargs(channel).items() if k in allowed}
    return yue.enhance_yue2_prompt(**kw, **prov)


QWI_EDIT = B.load("qwen_image21_edit")


def run_qwi(channel, prompt, mode, gallery, max_chars, ratio, transparent, profile, seed):
    out = call_filtered(
        qwi.QwenImage21PromptEnhancer.execute, prompt=prompt, input_mode=mode,
        reference_images=gallery_dict(gallery, qwi.MAX_IMAGES), max_output_chars=int(max_chars),
        wh_ratio=ratio, transparent_alpha=bool(transparent), rewrite_profile=profile, seed=int(seed),
        **provider_kwargs(channel))
    return tuple(out)[:4]



# ═════════════════════════ MOSS Image（節點 1.36+）═════════════════════════
try:
    MOSS = B.load("moss_image")
    MOSS_C = B.load("moss_image_contract")
    MOSS_R = B.load("moss_authoring_rules")
    MOSS_PROFILES = list(getattr(MOSS_R, "PROFILES", []))
except Exception as _e:  # noqa: BLE001
    MOSS = MOSS_C = MOSS_R = None
    MOSS_PROFILES = []
    _WARN.append(f"節點沒有 MOSS Image 模組，分頁會隱藏：{_e}")


def run_moss(channel, prompt, operation, fmt, width, height, lang, gallery, locked_text, max_chars,
             profile, max_corr, draft_json, seed):
    out = call_filtered(
        MOSS.MOSSImagePromptEnhancer.execute, prompt=prompt, operation=operation, output_format=fmt,
        width=int(width), height=int(height), output_language=lang,
        reference_images=gallery_dict(gallery, int(getattr(MOSS, "MAX_IMAGES", 10))), locked_text=locked_text,
        max_output_chars=int(max_chars), authoring_profile=profile, max_corrections=int(max_corr),
        draft_json=draft_json, seed=int(seed), **provider_kwargs(channel))
    vals = list(tuple(out))[:6]
    return vals[0], int(vals[1]), int(vals[2]), vals[3], vals[4], vals[5]


# ═════════════════════════ API 來源管理（設定頁互動）═════════════════════════
FORM_KEYS = ["name", "type", "base_url", "model", "api_key", "workshop_model", "strata_allow_private",
             "local_model", "local_mmproj", "local_context_size", "local_max_tokens", "local_think_mode",
             "local_unload_policy"]
TYPE_HINTS = {
    T_OPENAI: "LM Studio、llama-server、vLLM、NVIDIA（`https://integrate.api.nvidia.com/v1/chat/completions`）等。"
              "Base URL 可含或不含 `/chat/completions`。",
    T_LOCAL: "由節點啟動 llama-server 載入下方的 GGUF。llama-server 路徑在下方「本地 llama.cpp 與全域設定」。",
    T_SEEDANCE: "只需要 API Key；留空會讀環境變數 `SEEDANCE_API_KEY`。",
    T_WORKSHOP: "選擇 AI 工坊模型；選 Custom 時在「模型 ID」填完整模型名稱。",
    T_STRATA: "連到已經在執行的 Strata 服務（預設 `http://127.0.0.1:8080`）。模型留空或填 `AUTO` 會用服務目前載入的模型。"
              "Strata 在本機時 API Key 選填；**在其他電腦上時要勾選「允許區網位址」，而且必須填 API Key**。",
}


def type_visibility(t):
    show = {"url": t in (T_OPENAI, T_STRATA), "model": t in (T_OPENAI, T_WORKSHOP, T_STRATA),
            "key": t != T_LOCAL, "wmodel": t == T_WORKSHOP, "strata": t == T_STRATA, "local": t == T_LOCAL}
    url_label = "Strata 服務位址" if t == T_STRATA else "Base URL"
    model_label = {T_WORKSHOP: "自訂模型 ID（AI 工坊選 Custom 時）", T_STRATA: "模型（留空 = AUTO）"}.get(t, "模型 ID")
    key_label = {T_STRATA: "API Key（選填）", T_SEEDANCE: "API Key（留空讀 SEEDANCE_API_KEY）"}.get(t, "API Key")
    return [gr.update(visible=show["url"], label=url_label), gr.update(visible=show["model"], label=model_label),
            gr.update(visible=show["key"], label=key_label), gr.update(visible=show["wmodel"]),
            gr.update(visible=show["strata"]), gr.update(visible=show["local"]), TYPE_HINTS.get(t, "")]


def list_info():
    rows = [f"- {'★ ' if n == CFG['default_profile'] else ''}**{n}**　{get_profile(n)['type']}" for n in profile_names()]
    return "\n".join(rows)


def form_values(name):
    p = get_profile(name)
    return [p[k] for k in FORM_KEYS]


def radio_updates(*currents):
    names = profile_names()
    return [gr.Radio(choices=names, value=c if c in names else CFG["default_profile"]) for c in currents]


def _unique(base):
    names, n, name = profile_names(), 2, base
    while name in names:
        name, n = f"{base} {n}", n + 1
    return name


def wire_settings(ui):
    lst, form, vis = ui["list"], ui["form"], ui["vis"]
    radios = list(CHANNEL_RADIOS)
    f_type = form[1]
    f_type.change(type_visibility, f_type, vis)

    def load(name):
        vals = form_values(name)
        return [*vals, *type_visibility(vals[1]), ""]
    lst.change(load, lst, [*form, *vis, ui["msg"]])

    def save(selected, *vals):
        data = dict(zip(FORM_KEYS, vals))
        data["name"] = str(data["name"] or "").strip()
        if not data["name"]:
            return [gr.update(), *[gr.update()] * len(radios), gr.update(), "❌ 名稱不能空白"]
        if data["name"] != selected and data["name"] in profile_names():
            return [gr.update(), *[gr.update()] * len(radios), gr.update(), f"❌ 已經有名為「{data['name']}」的來源"]
        for k in ("local_context_size", "local_max_tokens"):
            data[k] = int(data[k])
        data["strata_allow_private"] = bool(data["strata_allow_private"])
        idx = next((i for i, p in enumerate(CFG["profiles"]) if p["name"] == selected), None)
        if idx is None:
            CFG["profiles"].append({**PROFILE_DEFAULTS, **data})
        else:
            CFG["profiles"][idx] = {**PROFILE_DEFAULTS, **data}
            if CFG["default_profile"] == selected:
                CFG["default_profile"] = data["name"]
        save_cfg()
        return [gr.Radio(choices=profile_names(), value=data["name"]), *radio_updates(*[None] * len(radios)),
                list_info(), f"✓ 已儲存「{data['name']}」"]
    ui["save"].click(save, [lst, *form], [lst, *radios, ui["info"], ui["msg"]])

    def add(copy_from=None):
        def fn(selected, *currents):
            src = get_profile(selected) if copy_from else {**PROFILE_DEFAULTS}
            name = _unique(f"{src['name']} 複本" if copy_from else "新來源")
            CFG["profiles"].append({**src, "name": name})
            save_cfg()
            return [gr.Radio(choices=profile_names(), value=name), *radio_updates(*currents), list_info()]
        return fn
    ui["new"].click(add(), [lst, *radios], [lst, *radios, ui["info"]])
    ui["dup"].click(add(True), [lst, *radios], [lst, *radios, ui["info"]])

    def star(selected, *currents):
        CFG["default_profile"] = selected
        save_cfg()
        return [*radio_updates(*currents), list_info()]
    ui["star"].click(star, [lst, *radios], [*radios, ui["info"]])

    def delete(selected, *currents):
        if len(CFG["profiles"]) <= 1:
            return [gr.update(), *[gr.update()] * len(radios), list_info() + "\n\n❌ 至少要保留一組來源"]
        CFG["profiles"] = [p for p in CFG["profiles"] if p["name"] != selected]
        if CFG["default_profile"] == selected:
            CFG["default_profile"] = CFG["profiles"][0]["name"]
        save_cfg()
        return [gr.Radio(choices=profile_names(), value=CFG["default_profile"]), *radio_updates(*currents), list_info()]
    ui["delete"].click(delete, [lst, *radios], [lst, *radios, ui["info"]])

    def test(*vals):
        d = dict(zip(FORM_KEYS, vals))
        t = d["type"]
        if t == T_LOCAL:
            return runtime_report()
        if t in (T_SEEDANCE, T_WORKSHOP):
            return "雲端渠道請直接在分頁按「👁 預覽完整請求」或生成一次測試。"
        base = _strata_url(d["base_url"]) + "/v1" if t == T_STRATA else \
            re.sub(r"/chat/completions/?$", "", (d["base_url"] or "").strip().rstrip("/"))
        if not base:
            return "❌ 請先填 Base URL"
        headers = {"Authorization": f"Bearer {d['api_key']}"} if d["api_key"] else {}
        try:
            r = requests.get(base + "/models", headers=headers, timeout=15)
        except requests.RequestException as e:
            return f"❌ 連不到 `{base}/models`：{type(e).__name__}"
        if r.status_code in (401, 403):
            return f"❌ HTTP {r.status_code}：API Key 錯誤或沒有權限"
        if r.status_code >= 400:
            return f"⚠ HTTP {r.status_code}：服務有回應，但不支援列出模型（不一定代表不能用）"
        try:
            ids = [m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)]
        except ValueError:
            return "⚠ 服務有回應，但回傳的不是 JSON"
        model = (d["model"] or "").strip()
        found = "" if not model or model.upper() == "AUTO" else \
            (f"\n\n✅ 模型 `{model}` 在清單中" if model in ids else f"\n\n⚠ 清單裡找不到模型 `{model}`，請確認 ID 是否正確")
        sample = "、".join(f"`{i}`" for i in ids[:15]) + ("…" if len(ids) > 15 else "")
        return f"✅ 連線成功，共 {len(ids)} 個模型：{sample}{found}"
    ui["test"].click(test, form, ui["msg"])

    def check(server, pyfb, mdir):
        CFG.update(llama_server_path=str(server or "").strip().strip('"'), allow_llama_cpp_python=bool(pyfb),
                   models_dir=mdir)
        set_models_dir(mdir)
        return runtime_report()
    ui["check"].click(check, [ui["server"], ui["pyfb"], ui["dir"]], ui["rt"])

    def scan(mdir):
        set_models_dir(mdir)
        m, mp = gguf_lists()
        return gr.Radio(choices=m, value=m[0] if m else None), gr.Radio(choices=mp, value=mp[0] if mp else None)
    ui["scan"].click(scan, ui["dir"], [form[7], form[8]])

    def gsave(server, pyfb, mdir, keys, conc):
        CFG.update(llama_server_path=str(server or "").strip().strip('"'), allow_llama_cpp_python=bool(pyfb),
                   models_dir=mdir, save_keys=bool(keys), concurrency=int(conc))
        set_models_dir(mdir)
        save_cfg()
        return f"✓ 已儲存到 `{CONFIG_PATH.name}`（同時執行數需重啟）"
    ui["gsave"].click(gsave, [ui["server"], ui["pyfb"], ui["dir"], ui["save_keys"], ui["conc"]], ui["gmsg"])


# ═════════════════════════ 介面 ═════════════════════════
def build_ui():
    CHANNEL_RADIOS.clear()
    with gr.Blocks(title="T8 Prompt Studio", **({} if _GR6 else {"css": CSS})) as demo:
        gr.Markdown("## T8 Prompt Studio　·　H3 / Music 3 / YuE2 / Qwen Image 2.1 / MOSS Image")
        with gr.Tabs():
            # ───────── ⚙ API 來源 ─────────
            with gr.Tab("⚙ API 來源"):
                gr.Markdown("可以存多組 API 來源（例如 LM Studio、NVIDIA、本地 GGUF…），每個分頁各自選用。"
                            "標 ★ 的是預設來源。")
                with gr.Row(equal_height=False):
                    with gr.Column(scale=1, min_width=260):
                        s_list = gr.Radio(profile_names(), value=CFG["default_profile"], label="來源清單")
                        with gr.Row():
                            s_new = gr.Button("＋ 新增", size="sm")
                            s_dup = gr.Button("⧉ 複製", size="sm")
                        with gr.Row():
                            s_star = gr.Button("★ 設為預設", size="sm")
                            s_del = gr.Button("🗑 刪除", size="sm", variant="stop")
                        s_info = gr.Markdown()
                    with gr.Column(scale=2, min_width=420):
                        p0 = get_profile(CFG["default_profile"])
                        f_name = gr.Textbox(label="名稱", value=p0["name"])
                        f_type = gr.Radio(list(PROVIDER_TYPES), value=p0["type"], label="類型")
                        f_url = gr.Textbox(label="Base URL", value=p0["base_url"],
                                           placeholder="https://integrate.api.nvidia.com/v1/chat/completions")
                        f_model = gr.Textbox(label="模型 ID", value=p0["model"])
                        f_key = gr.Textbox(label="API Key", value=p0["api_key"], type="password")
                        f_wmodel = gr.Radio(_opt(h3, "AI_WORKSHOP_MODEL_OPTIONS", [p0["workshop_model"]]),
                                            value=p0["workshop_model"], label="AI 工坊模型", visible=False)
                        f_strata = gr.Checkbox(value=bool(p0["strata_allow_private"]), visible=False,
                                               label="允許區網位址（Strata 不在本機、在其他電腦上時勾選）")
                        with gr.Group(visible=False) as f_local:
                            models, mmproj = gguf_lists()
                            f_lmodel = gr.Radio(models, value=_first(models, p0["local_model"]), label="GGUF 模型")
                            f_mmproj = gr.Radio(mmproj, value=_first(mmproj, p0["local_mmproj"]), label="視覺投影 mmproj")
                            f_ctx = gr.Slider(8192, 65536, value=int(p0["local_context_size"]), step=1024,
                                              label="上下文 Token")
                            f_max = gr.Slider(256, int(getattr(LOCAL_PV, "MAX_OUTPUT_TOKENS", 61440)),
                                              value=int(p0["local_max_tokens"]), step=256, label="單次生成 Token（含思考）")
                            thinks = list(getattr(qwi, "LOCAL_THINK_OPTIONS", None)
                                          or [LOCAL_PV.LOCAL_THINK_OFF, getattr(LOCAL_PV, "LOCAL_THINK_ON", "开启")])
                            f_think = gr.Radio(thinks, value=_first(thinks, p0["local_think_mode"]), label="思考模式")
                            unloads = _opt(LOCAL_PV, "LOCAL_UNLOAD_POLICIES", [p0["local_unload_policy"]])
                            f_unload = gr.Radio(unloads, value=_first(unloads, p0["local_unload_policy"]), label="卸載策略")
                        f_hint = gr.Markdown()
                        with gr.Row():
                            f_save = gr.Button("💾 儲存此來源", variant="primary")
                            f_test = gr.Button("🔌 測試連線")
                        f_msg = gr.Markdown()

                with gr.Accordion("本地 llama.cpp 與全域設定", open=False):
                    s_server = gr.Textbox(label="llama-server 執行檔路徑（例：C:\\llama.cpp\\llama-server.exe，也可填資料夾）",
                                          value=CFG["llama_server_path"])
                    s_pyfb = gr.Checkbox(value=bool(CFG["allow_llama_cpp_python"]),
                                         label="允許退回 llama-cpp-python（裝壞時會出錯，建議關閉）")
                    s_dir = gr.Textbox(label="模型根目錄（其下的 LLM/ 放 .gguf）", value=CFG["models_dir"])
                    with gr.Row():
                        s_check = gr.Button("🔍 檢查本地執行環境", size="sm")
                        s_scan = gr.Button("↻ 掃描 GGUF", size="sm")
                    s_rt = gr.Markdown()
                    with gr.Row():
                        s_save_keys = gr.Checkbox(value=bool(CFG["save_keys"]), label="把 API Key 存進設定檔（明文）")
                        s_conc = gr.Slider(1, 32, value=int(CFG["concurrency"]), step=1, label="最多同時執行數（重啟生效）")
                    s_gsave = gr.Button("💾 儲存全域設定")
                    s_gmsg = gr.Markdown()

                profile_form = [f_name, f_type, f_url, f_model, f_key, f_wmodel, f_strata,
                                f_lmodel, f_mmproj, f_ctx, f_max, f_think, f_unload]
                settings_ui = dict(list=s_list, form=profile_form, vis=[f_url, f_model, f_key, f_wmodel, f_strata,
                                                                        f_local, f_hint],
                                   new=s_new, dup=s_dup, star=s_star, delete=s_del, info=s_info, save=f_save,
                                   test=f_test, msg=f_msg, server=s_server, pyfb=s_pyfb, dir=s_dir,
                                   check=s_check, scan=s_scan, rt=s_rt, save_keys=s_save_keys, conc=s_conc,
                                   gsave=s_gsave, gmsg=s_gmsg)

            # ───────── MiniMax H3 ─────────
            with gr.Tab("MiniMax H3"):
                with gr.Row(equal_height=False):
                    with gr.Column(scale=2, min_width=260):
                        tpl = template_column("流程压缩｜动作载体接力到目的地兑现")
                    with gr.Column(scale=4, min_width=420):
                        gr.Markdown("### ② 參考資料")
                        ch = channel_radio()
                        prompt = gr.Textbox(label="視頻創意 / 提示詞", lines=3)
                        task = gr.Radio(H3_TASKS, value=_first(H3_TASKS, prefix="Ref2VA"), label="生成類型")
                        with gr.Row():
                            first = gr.Image(type="pil", label="首幀", height=150)
                            last = gr.Image(type="pil", label="尾幀", height=150)
                        gal = gr.Gallery(type="pil", label="參考圖（Ref2VA / Hybrid，最多 9 張，依序為 Picture 1…）",
                                         interactive=True, columns=5, height=170)
                        with gr.Row():
                            duration = gr.Slider(1, 30, value=10, step=1, label="時長（秒）")
                            shots = gr.Slider(0, 20, value=0, step=1, label="鏡頭數（0 = AUTO）")
                        rewrite = gr.Radio(_opt(h3, "REWRITE_MODES", ["strict", "balanced", "creative"]),
                                           value="balanced", label="改寫模式")
                        langs = _opt(h3, "OUTPUT_LANGUAGES", ["中文", "English"])
                        lang = gr.Radio(langs, value=_first(langs, "English"), label="輸出語言")
                        rules = rules_block()
                        with gr.Accordion("進階設定", open=False):
                            words = gr.Slider(0, 1000, value=0, step=10, label="目標長度（0 = 自動）")
                            presets = _opt(h3, "CREATIVE_PRESET_OPTIONS", ["AUTO"])
                            preset = gr.Radio(presets, value=_first(presets, _opt(h3, "AUTO_CREATIVE_PRESET", None)),
                                              label="MiniMax 官方場景 Skill")
                            dirs = _opt(h3, "DIRECTOR_OPTIONS", ["关闭 / Off"])
                            director = gr.Radio(dirs, value=dirs[0], label="定向創作 Skill")
                            quals = _opt(h3, "QUALITY_OPTIONS", ["关闭 / Off"])
                            quality = gr.Radio(quals, value=_first(quals, prefix="质量纠正"), label="輸出質量流程")
                            creas = _opt(h3, "CREATION_OPTIONS", ["原有编排 / Original"])
                            creation = gr.Radio(creas, value=creas[0], label="動作編排")
                            profs = _opt(h3, "OFFICIAL_SKILL_PROFILES", ["现有兼容（保留中英文）"])
                            profile = gr.Radio(profs, value=_first(profs, _opt(h3, "COMPAT_SKILL_PROFILE", None)),
                                               label="H3 核心 Skill 協議")
                            pmode = gr.Radio(H3_PROMPT_MODES, value=H3_PROMPT_MODES[0], label="提示詞模式")
                            ref_template = gr.Textbox(label="參考模板（参考模板融合 時必填）", lines=2)
                            ref_ctx = gr.Textbox(label="參考素材補充（例：<Picture 1>=人物；<Picture 2>=場景）", lines=2)
                            constraints = gr.Textbox(label="硬性要求", lines=2)
                            vurls = gr.Textbox(label="參考影片公開 URL（OpenAI 相容渠道，每行一個）", lines=2)
                            seed = gr.Number(label="seed", value=0, precision=0)
                        with gr.Accordion("視覺風格與分鏡（實驗）", open=False, visible=bool(STYLE_OPTIONS or STORYBOARD_OPTIONS)):
                            gr.Markdown("色彩風格、創作者風格各選一個（選「未选择 / None」= 關閉），兩者可同時使用。")
                            c_q = gr.Textbox(placeholder="搜尋色彩風格（編號或名稱）…", show_label=False, visible=bool(STYLE_OPTIONS))
                            color_sel = gr.Radio(STYLE_OPTIONS, value=STYLE_NONE, label="色彩風格",
                                                 elem_classes="tpl-list", visible=bool(STYLE_OPTIONS))
                            color_scope = gr.Radio(STYLE_SCOPES, value=STYLE_SCOPES[0] if STYLE_SCOPES else None,
                                                   label="色彩範圍", visible=bool(STYLE_SCOPES))
                            k_q = gr.Textbox(placeholder="搜尋創作者風格（編號或名稱）…", show_label=False, visible=bool(STYLE_OPTIONS))
                            creator_sel = gr.Radio(STYLE_OPTIONS, value=STYLE_NONE, label="創作者風格",
                                                   elem_classes="tpl-list", visible=bool(STYLE_OPTIONS))
                            storyboard = gr.Radio(STORYBOARD_OPTIONS, value=STORYBOARD_OPTIONS[0] if STORYBOARD_OPTIONS else None,
                                                  label="分鏡導演", visible=bool(STORYBOARD_OPTIONS))
                            c_q.change(filter_styles, [c_q, color_sel], color_sel)
                            k_q.change(filter_styles, [k_q, creator_sel], creator_sel)
                        btn = gr.Button("運行提示詞優化", variant="primary", size="lg")
                        out = copy_box(label="enhanced_prompt", lines=20)
                        pv, full = request_panel()
                    with gr.Column(scale=4, min_width=360):
                        show_fn, show_outs = showcase_column(tpl, prompt, "h3")
                rule_sel, rule_target, rule_view = rules
                for c in (rule_sel, rule_target):
                    c.change(lambda n, p, t: rules_preview(n, p, t, "h3"), [rule_sel, prompt, rule_target], rule_view)
                demo.load(show_fn, [tpl, prompt], show_outs)
                wire(btn, pv, run_h3,
                     [ch, tpl, prompt, task, first, last, gal, duration, shots, rewrite, lang, words, preset,
                      director, quality, creation, profile, pmode, ref_template, ref_ctx, constraints, vurls, seed,
                      rule_sel, rule_target, color_sel, color_scope, creator_sel, storyboard], [out], full)

            # ───────── MiniMax Music 3 ─────────
            with gr.Tab("Music 3"):
                with gr.Row(equal_height=False):
                    with gr.Column(scale=1, min_width=420):
                        ch3 = channel_radio()
                        idea = gr.Textbox(label="音樂想法 / Music idea", lines=3)
                        lmodes = _opt(m3, "LYRICS_MODES", ["AUTO"])
                        lyrics_mode = gr.Radio(lmodes, value=lmodes[0], label="歌詞模式")
                        lyrics = gr.Textbox(label="歌詞（保留／潤色時填寫）", lines=8)
                        llangs = _opt(m3, "LYRICS_LANGUAGES", ["AUTO"])
                        lyrics_lang = gr.Radio(llangs, value=llangs[0], label="歌詞語言")
                        custom_lang = gr.Textbox(label="自訂歌詞語言", visible=True)
                        qms = _opt(m3, "QUALITY_MODES", ["快速"])
                        quality3 = gr.Radio(qms, value=_first(qms, _opt(m3, "FULL_QUALITY_MODE", None)), label="質量流程")
                        with gr.Row():
                            m_duration = gr.Slider(0, 600, value=0, step=5, label="目標時長（秒，0 = 自動）")
                            bpm = gr.Slider(0, 240, value=0, step=1, label="BPM（0 = 自動）")
                        with gr.Accordion("結構、潤色與進階", open=False):
                            rewrite3 = gr.Radio(["strict", "balanced", "creative"], value="balanced", label="改寫幅度")
                            sps = _opt(m3, "STRUCTURE_PRESETS", ["AUTO"])
                            structure = gr.Radio(sps, value=sps[0], label="歌曲結構")
                            custom_structure = gr.Textbox(label="自訂結構")
                            edit_req = gr.Textbox(label="潤色要求", lines=2)
                            scopes = _opt(m3, "EDIT_SCOPE_OPTIONS", ["AUTO"])
                            edit_scope = gr.Radio(scopes, value=scopes[0], label="潤色範圍")
                            secs = _opt(m3, "EDIT_SECTION_OPTIONS", ["Chorus"])
                            edit_section = gr.Radio(secs, value=secs[min(1, len(secs) - 1)], label="段落")
                            edit_occ = gr.Number(label="第幾次出現（0 = 全部）", value=0, precision=0)
                            constraints3 = gr.Textbox(label="約束與排除", lines=2)
                            key_scale = gr.Textbox(label="調性（例：A minor）")
                            meters = _opt(m3, "METERS", ["AUTO", "4/4", "3/4", "6/8"])
                            meter = gr.Radio(meters, value=meters[0], label="拍號")
                            custom_meter = gr.Textbox(label="自訂拍號")
                            cls_ = _opt(m3, "CAPTION_LANGUAGES", ["English"])
                            caption_lang = gr.Radio(cls_, value=cls_[0], label="Caption 語言")
                            caption_words = gr.Slider(0, 400, value=0, step=10, label="Caption 字數（0 = 自動）")
                            sems = _opt(m3, "SEMANTIC_PROFILE_MODES", ["隐私隔离"])
                            semantic = gr.Radio(sems, value=sems[0], label="歌詞語意畫像")
                            manual_profile = gr.Textbox(label="手動宽泛畫像", lines=2)
                            scs = _opt(m3, "STAGE_CACHE_OPTIONS", ["开启"])
                            stage_cache = gr.Radio(scs, value=scs[0], label="階段快取")
                            seed3 = gr.Number(label="seed", value=0, precision=0)
                        btn3 = gr.Button("生成歌詞與音樂描述", variant="primary", size="lg")
                    with gr.Column(scale=1, min_width=420):
                        o_lyrics = copy_box(label="lyrics", lines=14)
                        o_caption = copy_box(label="music_caption", lines=6)
                        with gr.Accordion("payload JSON / 報告", open=False):
                            o_payload = code_box(label="music3_payload_json", lines=10)
                            o_report = code_box(label="enhancement_report_json", lines=10)
                        pv3, full3 = request_panel()
                wire(btn3, pv3, run_m3,
                     [ch3, idea, lyrics_mode, lyrics, lyrics_lang, custom_lang, m_duration, rewrite3, quality3,
                      structure, custom_structure, edit_req, edit_scope, edit_section, edit_occ, constraints3, bpm,
                      key_scale, meter, custom_meter, caption_lang, caption_words, semantic, manual_profile,
                      stage_cache, seed3], [o_lyrics, o_caption, o_payload, o_report], full3)

            # ───────── YuE2（表單直接由節點的 DEFAULTS 產生，節點新增欄位會自動出現）─────────
            with gr.Tab("YuE2"):
                combos = {
                    "lyrics_mode": _opt(yue, "LYRIC_MODES", []), "lyrics_language": ["中文", "English", "日本語", "한국어"],
                    "cot": list(getattr(yue, "COT_MODES", {}) or []),
                    "quality_mode": [getattr(yue, "STANDARD", "标准"), getattr(yue, "REVIEW", "审校")],
                    "style_language": ["English", "中文"], "creativity": ["strict", "balanced", "creative"],
                    "abc_action": [getattr(yue, "ABC_KEEP", "保留"), getattr(yue, "ABC_STRIP", "去和弦")],
                    "abc_source": [getattr(yue, "ABC_GENERATE", "自动"), getattr(yue, "ABC_DOWNSTREAM", "下游")],
                    "meter": ["AUTO", "4/4", "3/4", "6/8", "7/8"],
                }
                labels = {"music_idea": "創作要求", "lyrics_mode": "歌詞處理", "lyrics_language": "歌詞語言",
                          "lyrics": "原有歌詞", "cot": "YuE2 譜面規劃", "quality_mode": "質量流程",
                          "seed": "創作種子", "style_language": "風格描述語言", "structure": "歌曲結構", "genre": "曲風",
                          "vocal": "人聲", "instruments": "樂器", "bpm": "BPM（0=自動）", "meter": "拍號",
                          "key_scale": "調性", "constraints": "約束與避免元素", "target_duration_seconds": "規劃時長（秒）",
                          "creativity": "創作幅度", "edit_section": "改詞段名", "edit_occurrence": "第幾次出現",
                          "edit_request": "改詞要求", "abc": "已有 ABC 樂譜", "abc_action": "ABC 處理",
                          "yue2_seed": "YuE2 音樂種子", "song_id": "導出標識", "cfg_scale": "YuE2 CFG（-1=官方）",
                          "llm_max_tokens": "雲端單次 Token", "abc_source": "未提供樂譜時"}
                multiline = {"music_idea", "lyrics", "constraints", "edit_request", "abc"}
                with gr.Row(equal_height=False):
                    with gr.Column(scale=1, min_width=420):
                        ch4 = channel_radio()
                        yue_inputs = []
                        adv_started = False
                        adv = None
                        for i, name in enumerate(YUE_FIELDS):
                            if i >= 8 and not adv_started:
                                adv = gr.Accordion("進階設定", open=False)
                                adv.__enter__()
                                adv_started = True
                            d = yue.DEFAULTS[name]
                            lab = labels.get(name, name)
                            if name in combos and combos[name]:
                                opts = combos[name]
                                comp = gr.Radio(opts, value=d if d in opts else opts[0], label=lab)
                            elif isinstance(d, bool):
                                comp = gr.Checkbox(value=d, label=lab)
                            elif isinstance(d, (int, float)):
                                comp = gr.Number(value=d, label=lab, precision=0 if isinstance(d, int) else None)
                            else:
                                comp = gr.Textbox(value=d, label=lab, lines=5 if name in multiline else 1)
                            yue_inputs.append(comp)
                        if adv_started:
                            adv.__exit__(None, None, None)
                        btn4 = gr.Button("生成風格與歌詞", variant="primary", size="lg")
                    with gr.Column(scale=1, min_width=420):
                        y_style = copy_box(label="style", lines=4)
                        y_lyrics = copy_box(label="lyrics", lines=14)
                        y_abc = code_box(label="abc", lines=8)
                        with gr.Accordion("YuE2 請求 JSON / 報告", open=False):
                            y_req = code_box(label="yue2_request_json", lines=10)
                            y_rep = code_box(label="creation_report_json", lines=10)
                        pv4, full4 = request_panel()
                wire(btn4, pv4, run_yue, [ch4, *yue_inputs], [y_style, y_lyrics, y_abc, y_req, y_rep], full4)

            # ───────── Qwen Image 2.1 ─────────
            with gr.Tab("Qwen Image 2.1"):
                with gr.Row(equal_height=False):
                    with gr.Column(scale=1, min_width=420):
                        ch5 = channel_radio()
                        prompt5 = gr.Textbox(label="圖像提示詞 / 編輯要求", lines=4)
                        modes = _opt(qwi, "INPUT_MODES", ["文生图"])
                        mode5 = gr.Radio(modes, value=modes[0], label="模式")
                        gal5 = gr.Gallery(type="pil", label=f"參考圖（圖像編輯，最多 {qwi.MAX_IMAGES} 張，依序為 <image1>…）",
                                          interactive=True, columns=5, height=200)
                        ratios = _opt(qwi, "RATIO_OPTIONS", ["auto"])
                        ratio = gr.Radio(ratios, value=ratios[0], label="比例")
                        with gr.Accordion("進階設定", open=False):
                            transparent = gr.Checkbox(label="透明背景 RGBA", value=False)
                            profs5 = _opt(QWI_EDIT, "PROFILE_OPTIONS", ["经典兼容 / Classic"])
                            profile5 = gr.Radio(profs5, value=profs5[0], label="編輯規則")
                            max_chars = gr.Slider(0, int(getattr(qwi, "MAX_PROMPT_CHARS", 12000)), value=0, step=100,
                                                  label="輸出字數上限（0 = 自動）")
                            seed5 = gr.Number(label="seed", value=0, precision=0)
                        btn5 = gr.Button("生成圖像提示詞", variant="primary", size="lg")
                    with gr.Column(scale=1, min_width=420):
                        q_prompt = copy_box(label="rewritten_prompt", lines=14)
                        q_ratio = gr.Textbox(label="wh_ratio")
                        with gr.Accordion("請求 JSON / 報告", open=False):
                            q_req = code_box(label="qwen_image_request_json", lines=10)
                            q_rep = code_box(label="enhancement_report_json", lines=10)
                        pv5, full5 = request_panel()
                wire(btn5, pv5, run_qwi, [ch5, prompt5, mode5, gal5, max_chars, ratio, transparent, profile5, seed5],
                     [q_prompt, q_ratio, q_req, q_rep], full5)

            # ───────── MOSS Image ─────────
            with gr.Tab("MOSS Image", visible=MOSS is not None):
                if MOSS is not None:
                    with gr.Row(equal_height=False):
                        with gr.Column(scale=1, min_width=420):
                            ch6 = channel_radio()
                            prompt6 = gr.Textbox(label="創作需求 / Brief", lines=4)
                            ops = [MOSS_C.GENERATE, MOSS_C.COMPILE]
                            op6 = gr.Radio(ops, value=ops[0], label="處理方式")
                            fmts = list(MOSS_C.FORMATS)
                            fmt6 = gr.Radio(fmts, value=fmts[0], label="輸出格式（Caption 自然描述 / Layout 空間排版）")
                            with gr.Row():
                                w6 = gr.Number(label="寬（8 的倍數）", value=1024, precision=0)
                                h6 = gr.Number(label="高（8 的倍數）", value=1024, precision=0)
                            langs6 = list(MOSS_C.LANGUAGES)
                            lang6 = gr.Radio(langs6, value=langs6[0], label="描述語言")
                            prof6 = gr.Radio(MOSS_PROFILES, value=MOSS_PROFILES[0] if MOSS_PROFILES else None,
                                             label="強化模式", visible=bool(MOSS_PROFILES))
                            gal6 = gr.Gallery(type="pil", label="參考圖（最多 10 張；只給 LLM 參考，不是 MOSS 原生圖像編輯）",
                                              interactive=True, columns=5, height=170)
                            with gr.Accordion("進階設定", open=False):
                                locked6 = gr.Textbox(label="鎖定畫面文案（畫面上要出現的確切文字）", lines=2)
                                maxc6 = gr.Slider(0, int(MOSS_C.MAX_DESCRIPTION_CHARS), value=0, step=100,
                                                  label="最大描述字數（0 = 自動）")
                                corr6 = gr.Radio([0, 1], value=1, label="最多追加糾正次數")
                                draft6 = gr.Textbox(label="原始草稿 Draft JSON（處理方式選「仅校验编译」時使用）", lines=4)
                                seed6 = gr.Number(label="seed", value=0, precision=0)
                            btn6 = gr.Button("生成 MOSS 提示詞", variant="primary", size="lg")
                        with gr.Column(scale=1, min_width=420):
                            m_pos = copy_box(label="positive_prompt（接 MOSS 正向提示詞）", lines=14)
                            with gr.Row():
                                m_w = gr.Number(label="width", precision=0)
                                m_h = gr.Number(label="height", precision=0)
                                m_fmt = gr.Textbox(label="resolved_format")
                            with gr.Accordion("可編輯草稿 / 診斷報告", open=False):
                                m_draft = code_box(label="draft_json", lines=10)
                                m_rep = code_box(label="report_json", lines=10)
                            pv6, full6 = request_panel()
                    wire(btn6, pv6, run_moss, [ch6, prompt6, op6, fmt6, w6, h6, lang6, gal6, locked6, maxc6, prof6,
                                                corr6, draft6, seed6], [m_pos, m_w, m_h, m_fmt, m_draft, m_rep], full6)
        wire_settings(settings_ui)
        demo.load(lambda: [*type_visibility(get_profile(CFG["default_profile"])["type"]), list_info()],
                  None, [*settings_ui["vis"], settings_ui["info"]])
    return demo


def main():
    ap = argparse.ArgumentParser(description="T8 Prompt Studio（脫離 ComfyUI 的 T8 提示詞增強 WebUI）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7861)
    ap.add_argument("--no-browser", action="store_true", help="啟動時不要自動開啟瀏覽器")
    args = ap.parse_args()
    for w in _WARN:
        print("[相容]", w)
    demo = build_ui()
    demo.queue(default_concurrency_limit=int(CFG.get("concurrency", 8)))
    demo.launch(server_name=args.host, server_port=args.port, inbrowser=not args.no_browser,
                allowed_paths=[str(PREVIEW_TMP), str(RULES_DIR)],
                **({"css": CSS} if _GR6 else {}))


if __name__ == "__main__":
    main()
