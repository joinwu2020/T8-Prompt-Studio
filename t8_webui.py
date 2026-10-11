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

CH_OPENAI = "OpenAI 相容（LM Studio / llama-server）"
CH_LOCAL = "本地 GGUF（節點內建 llama.cpp）"
CH_SEEDANCE = "貞貞平價小屋"
CH_WORKSHOP = "貞貞的 AI 工坊"
CHANNELS = {
    CH_OPENAI: h3.OPENAI_API_MODE,
    CH_LOCAL: getattr(h3, "LOCAL_QWEN_API_MODE", "本地 GGUF（llama.cpp / Qwen，离线）"),
    CH_SEEDANCE: h3.SEEDANCE_API_MODE,
    CH_WORKSHOP: h3.AI_WORKSHOP_API_MODE,
}
DEFAULT_CFG = {
    "channel": CH_OPENAI,
    "openai_base_url": "http://192.168.0.53:1234/v1/chat/completions",
    "openai_model": "gemma-4-12b",
    "openai_key": "12345678",
    "seedance_key": "",
    "workshop_key": "",
    "workshop_model": getattr(h3, "AI_WORKSHOP_DEFAULT_MODEL", "gemini-3.5-flash"),
    "workshop_custom_model": "",
    "models_dir": str(B.MODELS_DIR),
    "local_model": LOCAL_RT.DEFAULT_MODEL_FILENAME,
    "local_mmproj": getattr(LOCAL_RT, "AUTO_MMPROJ", LOCAL_RT.DEFAULT_MMPROJ_FILENAME),
    "local_context_size": LOCAL_PV.DEFAULT_CONTEXT_SIZE,
    "local_max_tokens": LOCAL_PV.DEFAULT_MAX_TOKENS,
    "local_think_mode": LOCAL_PV.LOCAL_THINK_OFF,
    "local_unload_policy": LOCAL_PV.LOCAL_UNLOAD_AFTER_RUN,
    "llama_server_path": "",
    "allow_llama_cpp_python": False,
    "concurrency": 8,
    "save_keys": True,
}


def load_cfg() -> dict:
    cfg = dict(DEFAULT_CFG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


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
        models, mmproj = [CFG["local_model"]], [CFG["local_mmproj"]]
    return models, mmproj


def provider_kwargs(channel: str) -> dict:
    """依渠道產生節點共用的 API 參數；各節點只會收到它認得的那些"""
    c = CFG
    mode = CHANNELS.get(channel, h3.OPENAI_API_MODE)
    kw = {
        "api_mode": mode,
        "local_model": c["local_model"], "local_mmproj": c["local_mmproj"],
        "local_context_size": int(c["local_context_size"]), "local_max_tokens": int(c["local_max_tokens"]),
        "local_think_mode": c["local_think_mode"], "local_unload_policy": c["local_unload_policy"],
    }
    if channel == CH_OPENAI:
        kw.update(api_key=c["openai_key"], openai_base_url=c["openai_base_url"], custom_model=c["openai_model"])
    elif channel == CH_SEEDANCE:
        kw.update(api_key=c["seedance_key"])
    elif channel == CH_WORKSHOP:
        kw.update(api_key=c["workshop_key"], ai_workshop_model=c["workshop_model"],
                  custom_model=c["workshop_custom_model"])
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


def channel_radio():
    return gr.Radio(list(CHANNELS), value=CFG["channel"], label="渠道（預設值在 ⚙ 渠道設定）")


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


# ═════════════════════════ 各節點執行函式 ═════════════════════════
H3_TASKS = _opt(h3, "TASK_TYPE_LABELS", ["Ref2VA（参考图/视频生音视频）"])
H3_PROMPT_MODES = _opt(h3, "PROMPT_MODES", ["官方增强", "参考模板融合"])


def run_h3(channel, tpl, prompt, task, first, last, gallery, duration, shots, rewrite, lang, words,
           preset, director, quality, creation, profile, prompt_mode, ref_template, ref_ctx, constraints,
           video_urls, seed, rule_names, rule_target):
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
        openai_video_urls=video_urls, seed=int(seed), **provider_kwargs(channel))


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
    allowed = set(yue.DEFAULTS) | {"api_key", "lyric_plan", "arrangement_plan"}
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


# ═════════════════════════ 介面 ═════════════════════════
def build_ui():
    with gr.Blocks(title="T8 Prompt Studio", **({} if _GR6 else {"css": CSS})) as demo:
        gr.Markdown("## T8 Prompt Studio　·　H3 / Music 3 / YuE2 / Qwen Image 2.1")
        with gr.Tabs():
            # ───────── ⚙ 渠道設定 ─────────
            with gr.Tab("⚙ 渠道設定"):
                s_channel = gr.Radio(list(CHANNELS), value=CFG["channel"], label="預設渠道（各分頁可個別切換）")
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("#### OpenAI 相容（LM Studio / llama-server / vLLM）")
                        s_url = gr.Textbox(label="Base URL", value=CFG["openai_base_url"])
                        s_model = gr.Textbox(label="模型 ID", value=CFG["openai_model"])
                        s_okey = gr.Textbox(label="API Key", value=CFG["openai_key"], type="password")
                        gr.Markdown("#### 雲端")
                        s_skey = gr.Textbox(label="貞貞平價小屋 API Key（留空讀 SEEDANCE_API_KEY）",
                                            value=CFG["seedance_key"], type="password")
                        s_wkey = gr.Textbox(label="AI 工坊 API Key", value=CFG["workshop_key"], type="password")
                        s_wmodel = gr.Radio(_opt(h3, "AI_WORKSHOP_MODEL_OPTIONS", [CFG["workshop_model"]]),
                                            value=CFG["workshop_model"], label="AI 工坊模型")
                        s_wcustom = gr.Textbox(label="AI 工坊自訂模型 ID", value=CFG["workshop_custom_model"])
                    with gr.Column():
                        gr.Markdown("#### 本地 GGUF（節點內建 llama.cpp，免 API Key）")
                        s_server = gr.Textbox(label="llama-server 執行檔路徑（例：C:\\llama.cpp\\llama-server.exe，也可填資料夾）",
                                              value=CFG["llama_server_path"])
                        s_pyfb = gr.Checkbox(value=bool(CFG["allow_llama_cpp_python"]),
                                             label="允許退回 llama-cpp-python（裝壞時會出錯，建議關閉）")
                        s_check = gr.Button("🔍 檢查本地執行環境", size="sm")
                        s_rt = gr.Markdown()
                        s_dir = gr.Textbox(label="模型根目錄（其下的 LLM/ 放 .gguf）", value=CFG["models_dir"])
                        models, mmproj = gguf_lists()
                        s_lmodel = gr.Radio(models, value=_first(models, CFG["local_model"]), label="模型")
                        s_mmproj = gr.Radio(mmproj, value=_first(mmproj, CFG["local_mmproj"]), label="視覺投影 mmproj")
                        s_scan = gr.Button("↻ 掃描 GGUF", size="sm")
                        s_ctx = gr.Slider(8192, 65536, value=int(CFG["local_context_size"]), step=1024, label="上下文 Token")
                        s_max = gr.Slider(256, int(getattr(LOCAL_PV, "MAX_OUTPUT_TOKENS", 61440)),
                                          value=int(CFG["local_max_tokens"]), step=256, label="單次生成 Token（含思考）")
                        thinks = list(getattr(qwi, "LOCAL_THINK_OPTIONS", None) or getattr(LOCAL_PV, "LOCAL_THINK_OPTIONS", None)
                                      or [LOCAL_PV.LOCAL_THINK_OFF, getattr(LOCAL_PV, "LOCAL_THINK_ON", "开启")])
                        s_think = gr.Radio(thinks, value=_first(thinks, CFG["local_think_mode"]), label="思考模式")
                        s_unload = gr.Radio(_opt(LOCAL_PV, "LOCAL_UNLOAD_POLICIES", [CFG["local_unload_policy"]]),
                                            value=CFG["local_unload_policy"], label="卸載策略")
                with gr.Row():
                    s_save_keys = gr.Checkbox(value=bool(CFG["save_keys"]), label="把 API Key 存進設定檔（明文）")
                    s_conc = gr.Slider(1, 32, value=int(CFG["concurrency"]), step=1, label="最多同時執行數（重啟生效）")
                s_btn = gr.Button("💾 儲存設定", variant="primary")
                s_msg = gr.Markdown()

                def save(channel, url, model, okey, skey, wkey, wmodel, wcustom, mdir, lmodel, mm, ctx, mx,
                         think, unload, save_keys, conc, server, pyfb):
                    CFG.update(llama_server_path=str(server or "").strip().strip('"'),
                               allow_llama_cpp_python=bool(pyfb))
                    CFG.update(channel=channel, openai_base_url=url, openai_model=model, openai_key=okey,
                               seedance_key=skey, workshop_key=wkey, workshop_model=wmodel,
                               workshop_custom_model=wcustom, models_dir=mdir, local_model=lmodel,
                               local_mmproj=mm, local_context_size=int(ctx), local_max_tokens=int(mx),
                               local_think_mode=think, local_unload_policy=unload, save_keys=bool(save_keys),
                               concurrency=int(conc))
                    set_models_dir(mdir)
                    data = dict(CFG)
                    if not save_keys:
                        for k in ("openai_key", "seedance_key", "workshop_key"):
                            data[k] = ""
                    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    return f"✓ 已儲存到 `{CONFIG_PATH.name}`，立即生效（同時執行數需重啟）。"

                def scan(mdir):
                    set_models_dir(mdir)
                    m, p = gguf_lists()
                    return gr.Radio(choices=m, value=_first(m, CFG["local_model"])), \
                        gr.Radio(choices=p, value=_first(p, CFG["local_mmproj"]))

                s_btn.click(save, [s_channel, s_url, s_model, s_okey, s_skey, s_wkey, s_wmodel, s_wcustom, s_dir,
                                   s_lmodel, s_mmproj, s_ctx, s_max, s_think, s_unload, s_save_keys, s_conc,
                                   s_server, s_pyfb], s_msg)

                def check(server, pyfb, mdir):
                    CFG.update(llama_server_path=str(server or "").strip().strip('"'),
                               allow_llama_cpp_python=bool(pyfb), models_dir=mdir)
                    set_models_dir(mdir)
                    return runtime_report()

                s_check.click(check, [s_server, s_pyfb, s_dir], s_rt)
                s_scan.click(scan, s_dir, [s_lmodel, s_mmproj])

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
                      rule_sel, rule_target], [out], full)

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
