import json
import logging
import os
import re
from dataclasses import dataclass

from backend.adapter.standard_request import CLAUDE_CODE_OPENAI_PROFILE, OPENCLAW_OPENAI_PROFILE
from backend.core.request_logging import get_request_context
from backend.services import file_content_cache
from backend.services.client_profiles import (
    QWEN_CODE_OPENAI_PROFILE,
    looks_like_opencode_system_prompt as _looks_like_opencode_system_prompt,
    sanitize_openclaw_user_text,
)
from backend.services.refusal_cleaner import clean_refusal_messages
from backend.services.schema_compressor import compact_schema
from backend.services.tool_few_shot import pick_few_shot_tools, render_few_shot_turn, tool_summary_for_log
from backend.services.tool_name_obfuscation import obfuscate_bare_names, to_qwen_name
from backend.services.topic_isolation import detect_topic_change
from backend.services.workspace_context import build_workspace_notice, derive_workspace_root
from backend.toolcall.formats_qnml import build_qnml_tool_instructions, render_qnml_tool_call

log = logging.getLogger("qwen2api.prompt")

OPENCLAW_STARTUP_PATTERNS = (
    "A new session was started via /new or /reset.",
    "If runtime-provided startup context is included for this first turn",
)
OPENCLAW_UNTRUSTED_METADATA_PREFIX = "Sender (untrusted metadata):"

_TOOL_INTENT_WORDS = (
    "read", "open", "search", "find", "grep", "edit", "write", "create", "generate", "save",
    "file", "folder", "code", "project", "run", "execute", "command", "shell", "web", "url",
    "\u8bfb", "\u8bfb\u53d6", "\u67e5\u770b", "\u6253\u5f00", "\u641c\u7d22", "\u67e5\u627e", "\u7f16\u8f91", "\u4fee\u6539", "\u5199", "\u521b\u5efa", "\u751f\u6210", "\u4fdd\u5b58",
    "\u6587\u4ef6", "\u76ee\u5f55", "\u4ee3\u7801", "\u9879\u76ee", "\u8fd0\u884c", "\u6267\u884c", "\u547d\u4ee4", "\u7ec8\u7aef", "\u7f51\u9875", "\u8054\u7f51",
)


@dataclass(slots=True)
class PromptBuildResult:
    prompt: str
    tools: list[dict]
    tool_enabled: bool
    workspace_root: str | None = None


def _is_heavy_tool_profile(client_profile: str) -> bool:
    return client_profile in {CLAUDE_CODE_OPENAI_PROFILE, QWEN_CODE_OPENAI_PROFILE}


def _is_long_tool_context_profile(client_profile: str) -> bool:
    return client_profile == OPENCLAW_OPENAI_PROFILE


def _truncate_inline(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def _looks_tool_related(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in _TOOL_INTENT_WORDS)


def _tool_prompt_priority(tool_name: str) -> tuple[int, str]:
    key = re.sub(r"[^a-z0-9]+", "", (tool_name or "").lower())
    preferred = {
        "read": 0,
        "bash": 1,
        "glob": 2,
        "grep": 3,
        "write": 4,
        "edit": 5,
        "webfetch": 6,
        "websearch": 7,
    }
    control = {"agent", "askuserquestion", "enterplanmode", "exitplanmode", "enterworktree", "exitworktree"}
    if key in preferred:
        return preferred[key], tool_name
    if key in control:
        return 90, tool_name
    return 20, tool_name


def _compact_history_tool_input(name: str, input_data: dict, client_profile: str) -> dict:
    if client_profile != CLAUDE_CODE_OPENAI_PROFILE or not isinstance(input_data, dict):
        return input_data
    compact = dict(input_data)
    large_text_keys = ("content", "new_string", "old_string", "insert_text", "text", "patch")
    # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鐟懊归悩宸劀缂傚秵鐗曢…鍧楁嚋闂堟稑顫囬梺?闂傚倸鍊搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熼悜妯荤叆闁哄鐗忛埀顒€绠嶉崕閬嶅箖閸啔娲敂閸曨偆鈧厼顪冮妶鍡橆梿妞ゎ偄顦靛顒勫焵椤掑嫭鈷掑〒姘ｅ亾闁逞屽墰閸嬫盯鎳熼娑欐珷妞ゆ牗绋撶粻楣冩煠瑜版帒浜伴柛銈嗙懇閺屽秹鎸婃径妯恍﹀銈庡亝缁诲牓銆佸鈧幃鈺呮惞椤愵偄鏅犻梻鍌氬€搁崐椋庣矆娓氣偓楠炲鏁撻悩铏珨濠碉紕鍋戦崐鏍垂闂堟耽娲Ω瑜庨～鏇㈡煙閻愵剚缍戠紒鍓佸仱閺岀喖鏌囬敃鈧獮妤佺箾閸涱喚澧甸柡宀嬬秬缁犳盯寮撮悙鏉挎憢闂備胶顭堥鍡涘礉濞嗘挸绠?160 闂傚倸鍊搁崐宄懊归崶顒€违闁逞屽墴閺屾稓鈧綆鍋呭畷灞炬叏婵犲啯銇濇い銏℃礋閺佹劙宕堕崜浣风礃缂傚倸鍊风拋鏌ュ磻?50
    for key in large_text_keys:
        value = compact.get(key)
        if isinstance(value, str) and len(value) > 50:
            compact[key] = f"[{len(value)} chars]"

    # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鐟懊归悩宸劀缂傚秵鐗曢…鍧楁嚋闂堟稑顫囬梺?闂傚倸鍊搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熼悜姗嗘畷闁搞倕鐭傞弻娑㈠箻濡も偓鐎氼厼鈻撴导瀛樷拺闁硅偐鍋涢崝妤呮煛閸涱喚绠樺瑙勬礃缁傛帞鈧綆鍋嗛崢鎾绘⒑閸涘﹦绠撻悗姘煎弮瀹曟娊鎮滃Ο璇插伎闂佹寧绻傚Λ妤佹叏瀹ュ鐓欐い鏃€顑欏鎰磼濡ゅ啫鏋涙い銏＄☉椤繈宕ｅΟ鐑樻啟缂?
    for key in ("file_path", "path", "pattern"):
        value = compact.get(key)
        if isinstance(value, str) and len(value) > 80:
            parts = value.replace('\\\\', '/').split('/')
            if len(parts) > 3:
                compact[key] = f".../{'/'.join(parts[-2:])}"

    if name in {"Write", "Edit", "NotebookEdit"}:
        preferred = {}
        for key in ("file_path", "path", "target_file", "filename", "old_string", "new_string", "content"):
            if key in compact:
                preferred[key] = compact[key]
        if preferred:
            compact = preferred
    return compact


def _render_history_tool_call(name: str, input_data: dict, client_profile: str) -> str:
    # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁撻悩鎻掔€梺缁樻尭缁ㄥ爼寮稿澶嬬叆婵犻潧妫Σ褰掓煕鐎ｎ剙鏋戦柕鍥у瀵粙鈥﹂幋婵囶唲闂佺懓鍚嬮悾顏堝礉瀹ュ鍋傞柕澶嗘櫆閸婄敻鏌ㄥ┑鍡涱€楅柛妯绘尦閺岋繝鍩€椤掑嫭鐒肩€广儱妫岄幏娲⒑閸︻収鐒炬繛瀵稿厴閸╁﹪寮撮悩鍨紡闂佸搫顦冲▔鏇熺墡濠电儑绲藉ú銈夋晝椤忓嫮鏆︽い鎰剁畱鍞梺闈涚箳婵櫕绔熼弴鐐嶆棃鎮╅棃娑楃捕闂佽绻戠换鍫ュ箖濮椻偓椤㈡棃宕奸悢鍝勫箞闂備礁鍟块幖顐﹀磹婵犳艾违闁圭儤姊荤壕鍏笺亜閺冨浂娼愭繛鍛攻閵囧嫰濮€閿涘嫧妲堝銈庡亝缁诲牓銆侀弴銏犖ч柛銉㈡櫅楠炩偓闂傚倸鍊搁崐鐑芥嚄閸洍鈧箓宕奸姀鈥冲簥闂佸湱澧楀妯绘償婵犲洦鐓涢悘鐐额嚙閸旀氨绱掗悩宕囧⒌闁哄矉绻濆畷鍫曞Ψ閵壯傛偅闂備礁鎲￠〃鍡樼箾婵犲洤钃熼柣鏃囨閻瑩鏌涜椤ㄥ繘鍩€椤掍緡娈滄鐐寸墵瀵爼骞嬮婵嗘儓闂?Qwen-safe 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁嶉崟顒佹闂佸啿鎼幊搴ｅ婵犳碍鐓曢柡鍥ュ妼閻忕姷绱掗悩宕囧⒌闁哄矉绻濆畷鍫曞Ψ閵壯傜棯闂備礁鎼幏瀣礈閻旂厧钃熸繛鎴欏灪閺呮粎绱撴担鑲℃垿鎯堝鎻?闂?fs_open_file / 闂傚倸鍊搁崐鐑芥嚄閸洍鈧箓宕奸姀鈥冲簥闂佺懓鐡ㄧ换鎰版嚀閸ф鐓曢柨鏃囶嚙楠炴牠鏌?闂?u_xxx闂傚倸鍊搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熷▎陇顕уú顓€佸鈧慨鈧柣姗€娼ф慨?
    # 闂傚倷娴囬褍顫濋敃鍌︾稏濠㈣埖鍔曠粻鏍煕椤愶絾绀€缁炬儳娼″娲敆閳ь剛绮旈幘顔藉剹婵°倕鎳忛崑锝夋煙椤撶喎绗掑┑鈥茬矙閹顫濋悡搴♀拫闂佸搫鏈惄顖炵嵁閸ヮ剙绀傞柛婵勫劚閸ゎ剟姊绘担鍛婃儓婵☆偅顨堥幑銏狀潨閳ь剙顕ｇ拠娴嬫婵☆垱绮庨崰鏍箖濠婂喚娼ㄩ柛鈩冿供濡囨⒑閼姐倕鏋戠紒顔肩У閸掑﹥绂掔€ｎ亝妲梺鍝勭▉閸樿偐绮婚鐐寸厱婵炴垵宕悘锛勨偓瑙勬礀椤︾敻寮婚弴鐔虹瘈闊洦绋掗宥呪攽?QNML闂傚倸鍊搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熸潏楣冩闁搞倖鍔栭妵鍕冀椤愵澀绮剁紓浣哄閸ㄥ爼寮婚悢椋庢殝闁瑰嘲鐭堝鑸电箾鐎涙鐭婄紓宥咃躬瀵鎮㈤悡搴ｇ暰閻熸粌绉瑰铏綇閵婏絼绨婚梺闈涚墕閹冲繘宕宠ぐ鎺撶厓闁芥ê顦藉Ο鈧Δ鐘靛仦閿曘垹鐣峰鍕闁告縿鍎洪崑褔姊洪懡銈呮瀾缂侇喗鎹囬妴鍌炴晝閸屾稑娈戦梺鍛婃尫缁€渚€宕瑰┑瀣厱妞ゆ劑鍊曢弸鍌炴煕鐎ｎ亷韬柟顔肩秺楠炰線骞掗幋婵愮€撮柣鐐寸瀹€绋款潖缂佹ɑ濯撮柛娑橈工閺嗗牆顪冮妶鍐ㄥ闁硅櫕鍔欓獮鎴﹀閻樻牗妫冨畷顏呮媴鐟欏嫭鐝﹂梻鍌欑閹测剝绗熷Δ鍛獥婵娉涢悡妯尖偓骞垮劚濡稓寮ч埀顒勬⒒閸屾氨澧涚紒瀣浮楠炴牠骞囬悧鍫㈠幗闂佹寧妫侀褔寮稿☉銏＄厓闁芥ê顦藉Ο鈧Δ鐘靛仦閿曘垽銆佸☉姗嗘僵妞ゆ劑鍩勫Λ婊堟⒒閸屾艾鈧绮堟笟鈧俊鍫曞箹娴ｅ摜鍝楅梻渚囧墮缁夌敻宕戦埡鍌樹簻闊洦鎸炬晶鏇㈡煟?
    return render_qnml_tool_call(
        to_qwen_name(name),
        _compact_history_tool_input(name, input_data, client_profile),
    )


def _build_tool_instruction_block(tools: list[dict], client_profile: str) -> str:
    # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁撻悩鎻掔€梺缁樻尭缁ㄥ爼寮稿澶嬬叆婵犻潧妫Σ褰掓煕鐎ｎ剙鏋戦柕鍥у瀵粙鈥﹂幋婵囶唲闂佺懓鍚嬮悾顏堝礉瀹ュ鍋傞柕澶嗘櫆閸婄敻鏌ㄥ┑鍡涱€楅柛妯绘尦閺岋繝鍩€椤掑嫭鐒肩€广儱妫岄幏娲⒑閸︻収鐒炬繛瀵稿厴閸╁﹪寮撮悩鍨紡闂佸搫顦冲▔鏇熺墡闂備礁鎼幊鎾斥枖濞戙埄鏁囬柛蹇曞帶缁剁偛鈹戦悩鎻掆偓鏄忋亹婢跺ň鏀介柣姗嗗枛閻忚鲸绻涙径瀣创闁轰礁鍟存俊鐑藉煛娴ｇ儤鐒炬繝鐢靛仦閸垶宕瑰ú顏呭亗闁绘柨鎽滅弧鈧繝鐢靛Т閸婃悂顢旈埡鍌樹簻闁靛鍎洪崕鎴犵磼?Qwen 闂傚倸鍊搁崐鐑芥倿閿曞倹鍎戠憸鐗堝笒缁€澶屸偓鍏夊亾闁逞屽墴閸┾偓妞ゆ帊绀侀崵顒勬煕閹惧瓨鐨戦柍褜鍓熷褔濡堕幖浣哄祦闁搞儺鍓欑痪褔鎮规笟顖滃帥婵″樊鍨堕弻锝嗘償閿濆棗娈岄柣搴㈠嚬閸犳寮茬捄浣曟棃宕ㄩ鐙呯串闂備浇顫夐崕鍏兼叏閵堝鐓曢柟瀵稿亼娴滄粓鏌￠崘銊モ偓鍛婄閻愵剛绠鹃柛娑卞枟缁€瀣煛?Qwen-safe 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁嶉崟顒佹闂佸啿鎼幊搴ｅ婵犳碍鐓曢柡鍥ュ妼閻忕姷绱掗悩宕囧⒌闁哄矉绻濆畷鍫曞Ψ閵壯傜棯闂備礁鎼幏瀣礈閻旂厧钃熸繛鎴欏灪閺呮粎绱撴担鑲℃垿鎯堝鎻?闂?fs_open_file 缂傚倸鍊搁崐鎼佸磹閻戣姤鍊块柨鏂垮⒔閻瑩鏌熷▎鈥崇湴閸旀垿宕洪埀顒併亜閹烘垵鈧崵澹曟總绋跨骇闁割偅绋戞俊濂告煕濠靛棙鎯堥柍?
    # 闂傚倸鍊峰ù鍥敋瑜庨〃銉╁传閵壯傜瑝閻庡箍鍎遍ˇ顖炲垂閸屾稓绠剧€瑰壊鍠曠花濠氭煛閸曗晛鍔滅紒缁樼洴楠炲鎮欑€靛憡顓婚梻浣告啞椤ㄥ棛鍠婂澶娢﹂柛鏇ㄥ灠閸愨偓闂侀潧臎閸曨偅鐝┑鐘垫暩閸嬫盯骞婇幇顓犵闁逞屽墴閺?tools 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁嶉崟顒佹濠德板€曢幊宀勫焵椤掆偓閸燁垰顕ラ崟顖氱疀妞ゆ垟鏂傞崕鐢稿蓟濞戙垹绠涢柕濠忛檮閻濇牕顪冮妶鍌涙珚妞ゃ儲鎹囬崺鈧い鎺嗗亾缂佺姴绉瑰畷鏇熸綇閳规儳浜炬慨妯煎帶閺嬨倗绱掗鍓у笡缂佸倹甯為埀顒婄秵閸嬪棝宕㈡禒瀣拺闁圭娴风粻鎾翠繆椤愶絿娲存鐐诧躬瀹曟﹢顢欓挊澶夌紦婵＄偑鍊栭悧顓犲緤閼恒儳顩查柟娈垮枓閸嬫挾鎲撮崟顒傦紭闂佺閰ｆ禍鍫曘€佸鈧幊婵嬪箥椤旂偓婢戦梻浣告惈濞层劍鍒婇鐐嶏絿绮氬娉乻er 闂傚倸鍊搁崐鐑芥嚄閸洍鈧箓宕奸姀鈥冲簥闂佸壊鍋侀崕杈╁閸ф绾ч柛顐亜娴滄牕霉濠婂棭娼愮紒缁樼洴楠炲鎮欓崹顐㈡珣婵＄偑鍊栧ú婵囥仈閹间礁绠為柕濞垮労濞笺劑鏌涢埄鍐炬當妞ゎ偀鏅犲娲川婵犲倸顫呴梺绋款儐閹歌崵鎹㈠┑瀣仺闂傚牊绋戞竟瀣磽閸屾氨小缂佽埖鑹鹃锝夊Ω閿旂晫绉堕梺鍐叉惈閸熶即鎮￠弬娆炬富闁靛牆妫涙晶閬嶆煕鐎ｎ偆娲撮柟顔界懄缁绘繈宕堕妸褍骞楅梻浣哥秺閸嬪﹪宕㈤懖鈺佺筏闁煎鍊楃壕濂告煟濡櫣浠涢柡鍡╁墴閺?
    qwen_tools: list[dict] = []
    names: list[str] = []
    tool_schemas: list[str] = []
    for tool in sorted(tools, key=lambda t: _tool_prompt_priority(str(t.get("name", "")) if isinstance(t, dict) else "")):
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        qwen_name = to_qwen_name(tool.get("name", ""))
        names.append(qwen_name)
        schema = tool.get("parameters") or tool.get("input_schema") or {}
        compacted_schema = compact_schema(schema) if isinstance(schema, dict) else str(schema or "{}")
        if isinstance(compacted_schema, str) and len(compacted_schema) > 700:
            compacted_schema = compacted_schema[:700] + "..."
        description = _truncate_inline(tool.get("description") or "No description available", 100)
        tool_schemas.append(
            f"Tool: {qwen_name}\n"
            f"Description: {description}\n"
            f"Parameters: {compacted_schema}"
        )
        qwen_tools.append({**tool, "name": qwen_name})

    instructions = build_qnml_tool_instructions(
        names,
        tool_schemas,
        heavy_profile=(client_profile == CLAUDE_CODE_OPENAI_PROFILE),
    )
    if client_profile == CLAUDE_CODE_OPENAI_PROFILE:
        prefix = "\n".join([
            "IMPORTANT: Reply in the same language as the user. User inputs Chinese -> respond in Chinese.",
            "IMPORTANT: When the user asks for multiple actions, complete all required actions without asking for confirmation.",
            "IMPORTANT: If a file result says 'Unchanged since last read', do not read the same file again.",
            "IMPORTANT: Prefer direct project tools for project work. Use Agent/task/scheduling/control tools only when they are clearly necessary for the current task context or explicitly requested; if uncertain, continue with direct tools.",
            "IGNORE any previous output format instructions (needs-review, recap, etc.).",
            "",
        ])
        instructions = prefix + instructions
    else:
        prefix = "\n".join([
            "IMPORTANT: Reply in the same language as the user. User inputs Chinese -> respond in Chinese.",
            "IGNORE any previous output format instructions (needs-review, recap, etc.).",
            "Use tools only when they are necessary to directly answer the CURRENT TASK.",
            "If you already know the answer, answer directly without any tool call.",
            "Do not explore filesystem, environment, or external resources unless directly required.",
            "",
        ])
        instructions = prefix + instructions
    return obfuscate_bare_names(instructions)

def _compact_system_reminders(text: str) -> str:
    """Compact system-reminder blocks to a short placeholder."""
    if not text or "<system-reminder>" not in text:
        return text

    def _compact(m: re.Match) -> str:
        body = m.group(1).strip()
        first_line = body.split("\n", 1)[0].strip()[:80]
        return f"[system-reminder: {first_line}...]" if first_line else "[system-reminder]"

    return re.sub(
        r"<system-reminder>([\s\S]*?)</system-reminder>",
        _compact,
        text,
        flags=re.IGNORECASE,
    )


def _strip_system_reminders(text: str) -> str:
    """Remove system-reminder blocks for task/topic detection."""
    if not text or "<system-reminder>" not in text:
        return text
    cleaned = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<system-reminder>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _sanitize_openclaw_user_text(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return cleaned
    if any(marker in cleaned for marker in OPENCLAW_STARTUP_PATTERNS):
        return ""
    if cleaned.startswith(OPENCLAW_UNTRUSTED_METADATA_PREFIX):
        match = re.search(r"\n\n(\[[^\n]+\]\s*[\s\S]*)$", cleaned)
        if match:
            cleaned = match.group(1).strip()
        else:
            return ""
    return cleaned


def _extract_user_text_only(content, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
    """Extract user text for task/topic detection, excluding system-reminder blocks."""
    if isinstance(content, str):
        stripped = _strip_system_reminders(content)
        return _sanitize_openclaw_user_text(stripped) if client_profile == OPENCLAW_OPENAI_PROFILE else stripped
    if isinstance(content, list):
        text_blocks = []
        for part in content:
            if not isinstance(part, dict) or part.get("type", "") != "text":
                continue
            block_text = _strip_system_reminders(part.get("text", ""))
            if client_profile == OPENCLAW_OPENAI_PROFILE:
                block_text = _sanitize_openclaw_user_text(block_text)
            if block_text:
                text_blocks.append(block_text)
        return "\n".join(text_blocks)
    return ""


def _extract_text(content, user_tool_mode: bool = False, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> str:
    if isinstance(content, str):
        compacted = _compact_system_reminders(content)
        return _sanitize_openclaw_user_text(compacted) if client_profile == OPENCLAW_OPENAI_PROFILE else compacted
    if isinstance(content, list):
        parts = []
        text_blocks = []
        other_parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type", "")
            if t == "text":
                block_text = _compact_system_reminders(part.get("text", ""))
                if client_profile == OPENCLAW_OPENAI_PROFILE:
                    block_text = _sanitize_openclaw_user_text(block_text)
                if block_text:
                    text_blocks.append(block_text)
            elif t == "tool_use":
                other_parts.append(_render_history_tool_call(part.get("name", ""), part.get("input", {}), client_profile))
            elif t == "tool_result":
                inner = part.get("content", "")
                tid = part.get("tool_use_id", "")
                if isinstance(inner, str):
                    other_parts.append(f"[Tool Result for call {tid}]\n{_compact_tool_result_body(inner)}\n[/Tool Result]")
                elif isinstance(inner, list):
                    texts = [p.get("text", "") for p in inner if isinstance(p, dict) and p.get("type") == "text"]
                    other_parts.append(f"[Tool Result for call {tid}]\n{_compact_tool_result_body(''.join(texts))}\n[/Tool Result]")
            elif t == "input_file":
                other_parts.append(f"[Attachment file_id={part.get('file_id','')} filename={part.get('filename','')}]")
            elif t == "input_image":
                other_parts.append(f"[Attachment image file_id={part.get('file_id','')} mime={part.get('mime_type','')}]")

        if user_tool_mode and text_blocks:
            parts.append(text_blocks[-1])
        else:
            parts.extend(text_blocks)
        parts.extend(other_parts)
        return "\n".join(p for p in parts if p)
    return ""


def _normalize_tool(tool: dict) -> dict:
    if tool.get("type") == "function" and "function" in tool:
        fn = tool["function"]
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        }
    return {
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema") or tool.get("parameters") or {},
    }


def _normalize_tools(tools: list) -> list:
    return [_normalize_tool(t) for t in tools if isinstance(t, dict)]


def _tool_param_hint(tool: dict) -> str:
    params = tool.get("parameters", {}) or {}
    if not isinstance(params, dict):
        return ""

    props = params.get("properties", {}) or {}
    if not isinstance(props, dict) or not props:
        return ""

    required = params.get("required", []) or []
    ordered_keys: list[str] = []
    for key in required:
        if key in props and key not in ordered_keys:
            ordered_keys.append(key)
    for key in props:
        if key not in ordered_keys:
            ordered_keys.append(key)

    shown = ordered_keys[:3]
    if not shown:
        return ""
    suffix = ", ..." if len(ordered_keys) > len(shown) else ""
    return f" input keys: {', '.join(shown)}{suffix}"


def _safe_preview(text: str, limit: int = 240) -> str:
    if not text:
        return ""
    return text
    # compact = " ".join(text.split())
    # return compact[:limit] + ("...[truncated]" if len(compact) > limit else "")


def _compact_tool_result_body(body: str, *, limit: int = 8000, head: int = 3000, tail: int = 1000) -> str:
    # 濠电姷鏁告慨鐑藉极閹间礁纾婚柣鎰▕閻掕姤绻涢崱妯虹仸鐎规洖寮舵穱濠囧Χ閸涱喖鐝旂紓鍌氱У閻楁粓鍩€椤掆偓缁犲秹宕曢柆宓ュ洭顢涢悙绮规嫽?闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鍦磼鐎ｎ偓绱╂繛宸簼閺呮煡鏌涘☉鍙樼凹闁诲骸顭峰娲濞戞氨鐤勯梺鎼炲姀瀹曞灚绂?tool_result闂傚倸鍊搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熺€涙绠ラ柣鎺曞Г缁绘稑鐣濋埀顒勫焵椤掑倸澧筽age_content闂傚倸鍊搁崐椋庢濮橆剦鐒界憸宥堢亱闂佸搫鍟悧濠囧磹閸ф鐓ラ柡鍥╁仜閳ь剛顭堟晥闁哄被鍎查悡銉︾節闂堟稒顥炴い銉︽尭闇?HTML闂傚倸鍊搁崐椋庢濮橆剦鐒界憸宥堢亱闂佸搫鍟崐褰掝敃閼恒儲鍙忔俊顖涘绾偓鎱ㄧ憴鍕弨婵﹥妞介、妤呭焵椤掑倻鐭撻悗闈涙憸鐏忕數鈧箍鍎遍ˇ浼存偂韫囨搩鐔嗛悹楦挎婢ф洟鏌涢弬璇测偓妤冩閹烘鍋愰柤纰卞墮閻撶喖姊洪崫鍕潶闁告柨鐭傞崺銉﹀緞婵犲孩寤洪梺绯曞墲椤嫰鏁傞悾宀€鐦堝┑鐐茬墕閻忔繈寮搁幘缁樼厸闁告侗鍠氶惌濠冦亜椤撶偞绌跨紒鐘崇☉閳藉螣绾拌鲸效濠碉紕鍋戦崐鏍礉閹达箑鍨傛繛宸憾閺佸洤鈹戦悩宕囶暡闁?KB闂?
    # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁撻悩顐熷亾閿曞倸鐐婃い鎺嗗亾缂佹劖顨婇弻鐔煎箥椤旂⒈鏆梺绋款儏椤戝顕ｉ崼鏇為唶婵炴垶锚椤绻?prompt 婵犵數濮烽弫鎼佸磻閻愬樊鐒芥繛鍡樻尭鐟欙箓鎮楅敐搴′簽闁崇懓绉电换娑橆啅椤旂粯鍠氶梺杞扮閿曨亪寮?MAX_CHARS 婵犵數濮烽。钘壩ｉ崨鏉戠；闁告侗鍙庨悢鍡樹繆椤栨氨姣為柛瀣尭椤繈鎮℃惔銏㈠綆闂備浇顕栭崰姘跺磻閹捐埖宕叉繝闈涙－濞尖晜銇勯幘璺轰粶濠㈢懓瀛╂穱濠囨倷椤忓嫧鍋撻弽顐ｆ殰闁圭儤鏌￠崑鎾愁潩閻撳骸绫嶉梺杞扮缁夊綊骞冮姀銈呯闁兼祴鏅涢獮鍫熺節濞堝灝鏋熼柨鏇楁櫊瀹曟粌鈻庨幘铏緢濠电偛妫欓幐濠氭偂閺囩喍绻嗘い鏍ㄧ箓閸氬綊鏌ｉ鐔锋诞闁哄备鍓濋幏鍛村传閵夋劑鍨介弻锝夋晲閸℃瑧鐤勯悗瑙勬礈閸犳牗淇婇幖浣肝ㄩ柕鍫濇媼濡粓姊婚崒娆掑厡闁告鍐胯€块弶鍫涘妽濞呯姵淇婇妶鍛仴濞存粌缍婇弻鏇熷緞閸℃ɑ鐝曢梺缁樻尰濞茬喖寮婚悢鐓庣畾鐟滃繘鏁嶅澶婃瀬闁割偅绺鹃弨浠嬫煟閹邦喖鍔嬮柨娑氬枛閺屾稑螖娴ｇ硶鏋欏Δ鐘靛仜濡繂鐣峰鈧、娆撴嚃閳哄﹥效濠碉紕鍋戦崐鏍ь潖婵犳艾纾婚柟鍓х帛閸婂灚銇勯幘璺轰汗闁衡偓娴犲鐓熼柟閭﹀墮缁狙囨煟韫囧鈧繈寮婚弴銏犻唶婵犻潧妫崝澶愭⒑閼恒儱鐏ユい锕傛涧椤繐煤椤忓嫪绱堕梺鍛婃处閸撴岸骞冨▎鎾粹拺閻庣櫢闄勫妯讳繆鐠恒劎纾介柛灞剧⊕瀹曞矂鏌熼鐣岀煀閾伙綁鎮规担鑺ョ彧闁哥偛顦扮换婵嬫偨闂堟稐娌梺鎼炲妽閸庡ジ骞楅锔解拺闁告稑锕ョ粈瀣磼閻樺磭澧电€殿喖顭烽弫鎰緞濡粯娅撻梻浣稿悑缁佹挳寮埡鍛櫜濠㈣泛顑呮禒鍝勵渻閵堝棛澧い銊ユ噺缁傚秵銈ｉ崘鈹炬嫽?
    # 闂?head+tail 婵犵數濮烽弫鎼佸磿閹寸姴绶ら柦妯侯棦濞差亝鏅滈柣鎰靛墮鎼村﹪姊洪崨濠冨闁稿鎹囬幊鎾诲锤濡や胶鍙嗛梺缁樻煥閹碱偄鐡紓鍌欒兌婵寰婃ィ鍐ㄎ﹂柛鏇ㄥ枤閻も偓闂佸湱鍋撳娆忊枍閵堝鈷戠紓浣姑粭鍌滅磼椤旂晫鎳囩€殿喖顭锋俊鎼佸Ψ閵忊槅娼旀繝鐢靛仜濡瑩宕硅ぐ鎺戠煑婵犻潧鐗忕壕钘壝归敐鍫燁仩閻㈩垱绋撻埀顒€鍘滈崑鎾绘煙闂傚顦︾紒鐘崇墵閺岀喖顢涢崱蹇撲壕闂佸搫顑呴柊锝夋偂椤愶箑鐐婇柕濠忓椤︻參姊洪幐搴ｂ姇缂佸甯為幑銏犫攽鐎ｎ亶娼婇梺鎸庣箓濡盯濡撮幇鐗堚拺閻庣櫢闄勫妯讳繆閻ｅ瞼纾奸柡鍐ㄥ€搁弸搴亜椤愶絿鐭掗柛鈹惧亾濡炪倖甯掔€氼喖鐣垫笟鈧弻鐔兼焽閿曗偓楠炴牜绱掗崜浣镐槐闁哄瞼鍠栭弻鍥晝閳ь剟鐛弽顓熺厱?闂傚倸鍊峰ù鍥х暦閻㈢纾婚柣鎰暩閻瑩鐓崶銊р槈缂佲偓婢舵劖鍊堕柣鎰仛濞呮洟鏌￠崱顓犵暤闁哄矉缍佸顕€宕堕妷銏犱壕闁逞屽墴閺屾稓鈧綆鍋呯亸鐢告煙閸欏灏︾€规洜鍠栭、姗€鎮╅幓鎺旂У缂傚倸鍊搁崐鎼佸磹閸濄儳鐭撻柟缁㈠枛缁犵姵鎱ㄥ璇蹭壕閻庢鍣崑鍕敇婵傜鐐婇柨鏃囨婵即姊绘担绋挎倯缂佷焦鎸冲鎻掆攽鐎ｅ灚鏅╅梺鍝勫暙閻楀﹪鍩涢幋婢濆綊宕楅懖鈺傚櫘缂備礁顑呴…鐑藉蓟閿濆鍋嗛柛灞剧矌閺嗙娀姊洪幐搴㈢８闁搞劋绮欓妴浣糕槈濮楀棛鍙嗛梺閫炲苯澧撮柟顔惧仦缁绘繈宕堕妸褍骞楅梻渚€鈧稑宓嗘繛浣冲嫭娅犳い鏂款潟娴滄粓鏌曟径娑橆洭缂佺姷鍋為幈銊︾節閸愨斂浠㈠Δ鐘靛仦閸旀牠骞嗛弮鍫熸櫜闁糕剝顨嗛悵顖氣攽?闂傚倸鍊搁崐椋庣矆娴ｉ潻鑰块弶鍫氭櫅閸ㄦ繃銇勯弽顐粶缂佲偓婢跺绻嗛柕鍫濇噺閸ｇ懓顩奸崨顓涙斀妞ゆ梻鐡旈悞鐐箾婢跺顬肩紒?
    if not body or len(body) <= limit:
        return body
    dropped = len(body) - head - tail
    return f"{body[:head]}\n...[truncated {dropped} bytes from middle]...\n{body[-tail:]}"


def _message_contains_tool_result(message: dict) -> bool:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") in {"tool_result", "function_call_output"}
        for part in content
    )


def _latest_message_is_tool_result(messages: list, client_profile: str) -> bool:
    for message in reversed(messages or []):
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if message.get("role") == "tool":
            return True
        if message.get("role") == "user":
            user_text = _extract_user_text_only(content, client_profile=client_profile).strip()
            if _message_contains_tool_result(message) and not user_text:
                return True
            if user_text:
                return False
        elif message.get("role") == "assistant":
            return False
    return False


def _build_tool_result_followup_notice(messages: list, tools: list, client_profile: str) -> str:
    if not messages or not tools or client_profile != CLAUDE_CODE_OPENAI_PROFILE:
        return ""
    if not _latest_message_is_tool_result(messages, client_profile):
        return ""
    return (
        "[STATE NOTICE: MUST OBEY]\n"
        "The latest client message is a tool result, not a new user request.\n"
        "Use that result to continue from the current state or finish the task.\n"
        "If the latest result reports a successful Write/Edit/NotebookEdit, do NOT repeat the exact same write/edit payload for the same target; only write again when the new payload changes or completes the file.\n"
        "Do NOT restart the original task merely because it appears earlier in the prompt."
    )


def _clip_text(text: str, limit: int, suffix: str = "...[truncated]") -> str:
    if not isinstance(text, str):
        text = str(text or "")
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(suffix))
    return text[:keep].rstrip() + suffix


def _history_window_limit(tools: list, client_profile: str) -> int:
    if not tools:
        return 200
    if not _is_long_tool_context_profile(client_profile):
        return 30 if client_profile == CLAUDE_CODE_OPENAI_PROFILE else 8
    default = 60
    raw = os.getenv("QWEN_TOOL_HISTORY_WINDOW", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("[Prompt] invalid QWEN_TOOL_HISTORY_WINDOW=%r; using default=%d", raw, default)
        return default
    return max(8, min(value, 200))


def _build_system_prompt_block(system_prompt: str, tools: list, client_profile: str) -> str:
    system_prompt = (system_prompt or "").strip()
    if not system_prompt:
        return ""
    if tools and client_profile == CLAUDE_CODE_OPENAI_PROFILE:
        return ""
    if tools and _is_long_tool_context_profile(client_profile):
        return (
            "<SYSTEM INSTRUCTIONS - HIGHEST PRIORITY>\n"
            f"{_clip_text(system_prompt, 4000, suffix='...[system truncated]')}\n"
            "</SYSTEM INSTRUCTIONS>"
        )
    return f"<system>\n{_clip_text(system_prompt, 2000, suffix='...[system truncated]')}\n</system>"


def _first_user_task_text(messages: list, client_profile: str) -> str:
    for message in messages or []:
        if isinstance(message, dict) and message.get("role") == "user":
            text = _extract_user_text_only(message.get("content", ""), client_profile=client_profile).strip()
            if text:
                return text
    return ""


def _latest_user_task_text(messages: list, client_profile: str) -> str:
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
            text = _extract_user_text_only(message.get("content", ""), client_profile=client_profile).strip()
            if text:
                return text
    return ""


def _message_tool_result_summaries(message: dict, client_profile: str) -> list[str]:
    if not isinstance(message, dict):
        return []
    role = message.get("role", "")
    content = message.get("content", "")
    if role == "tool":
        if isinstance(content, list):
            body = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            body = content if isinstance(content, str) else str(content or "")
        tool_call_id = message.get("tool_call_id", "")
        prefix = f"id={tool_call_id} " if tool_call_id else ""
        return [f"{prefix}{_safe_preview(body, 360)}"]

    if not isinstance(content, list):
        return []

    summaries: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in {"tool_result", "function_call_output"}:
            continue
        body = part.get("content", "")
        if isinstance(body, list):
            body_text = "\n".join(
                item.get("text", "")
                for item in body
                if isinstance(item, dict) and item.get("type") == "text"
            )
        else:
            body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        tool_call_id = part.get("tool_use_id") or part.get("call_id") or part.get("id") or ""
        prefix = f"id={tool_call_id} " if tool_call_id else ""
        summaries.append(f"{prefix}{_safe_preview(body_text, 360)}")
    return summaries


def _message_tool_call_summaries(message: dict, client_profile: str) -> list[str]:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return []
    summaries: list[str] = []
    content = message.get("content", "")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool_use":
                continue
            name = part.get("name", "")
            tool_id = part.get("id", "")
            tool_input = part.get("input", {})
            hint = ""
            if isinstance(tool_input, dict):
                for key in ("file_path", "path", "command", "pattern"):
                    value = tool_input.get(key)
                    if isinstance(value, str) and value:
                        hint = f" {key}={_clip_text(value, 80)}"
                        break
            summaries.append(f"{name or 'tool'} id={tool_id}{hint}".strip())

    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        fn = tool_call.get("function", {}) or {}
        name = fn.get("name", "")
        call_id = tool_call.get("id", "")
        args = fn.get("arguments", "")
        summaries.append(f"{name or 'tool'} id={call_id} args={_safe_preview(args, 180)}".strip())
    return summaries


def _extract_latest_tool_result_summary(messages: list, client_profile: str) -> str:
    for message in reversed(messages or []):
        summaries = _message_tool_result_summaries(message, client_profile)
        if summaries:
            return summaries[-1]
    return ""


def _collect_recent_tool_activity(messages: list, client_profile: str, limit: int = 8) -> list[str]:
    activity: list[str] = []
    for message in reversed(messages or []):
        if not isinstance(message, dict):
            continue
        result_summaries = _message_tool_result_summaries(message, client_profile)
        for summary in reversed(result_summaries):
            activity.append(f"result: {summary}")
            if len(activity) >= limit:
                return list(reversed(activity))

        call_summaries = _message_tool_call_summaries(message, client_profile)
        for summary in reversed(call_summaries):
            activity.append(f"call: {summary}")
            if len(activity) >= limit:
                return list(reversed(activity))
    return list(reversed(activity))


def _count_tool_events(messages: list, client_profile: str) -> tuple[int, int]:
    calls = 0
    results = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        calls += len(_message_tool_call_summaries(message, client_profile))
        results += len(_message_tool_result_summaries(message, client_profile))
    return calls, results


def _build_task_memory_block(messages: list, tools: list, client_profile: str) -> str:
    if not messages or not tools or not _is_long_tool_context_profile(client_profile):
        return ""

    original_goal = _first_user_task_text(messages, client_profile)
    current_goal = _latest_user_task_text(messages, client_profile)
    latest_tool_result = _extract_latest_tool_result_summary(messages, client_profile)
    recent_activity = _collect_recent_tool_activity(messages, client_profile)
    tool_call_count, tool_result_count = _count_tool_events(messages, client_profile)

    lines = [
        "<TASK MEMORY - DO NOT DROP>",
        "This block is stable task memory for long tool chains.",
        "RAW HISTORY POLICY: The raw transcript may be windowed; this TASK MEMORY carries the task across unlimited tool turns.",
        f"TOOL PROGRESS: {tool_call_count} tool call(s), {tool_result_count} tool result(s) observed so far.",
    ]
    if original_goal:
        lines.append(f"ORIGINAL GOAL: {_clip_text(original_goal, 1200, suffix='...[original goal truncated]')}")
    if current_goal and current_goal != original_goal:
        lines.append(f"CURRENT USER GOAL: {_clip_text(current_goal, 900, suffix='...[current goal truncated]')}")
    if latest_tool_result:
        lines.append(f"LATEST TOOL RESULT: {_clip_text(latest_tool_result, 900, suffix='...[latest tool result truncated]')}")
    if recent_activity:
        lines.append("RECENT TOOL ACTIVITY:")
        lines.extend(f"- {_clip_text(item, 260)}" for item in recent_activity)
    lines.append("RULE: Continue from the latest tool result and original goal. Do not restart, forget the task, or switch to review/summary unless the user asked for that.")
    lines.append("</TASK MEMORY>")
    return "\n".join(lines)


def _build_dropped_history_summary(original_messages: list, kept_messages: list, tools: list, client_profile: str) -> str:
    if not original_messages or not tools or not _is_long_tool_context_profile(client_profile):
        return ""
    dropped = max(0, len(original_messages) - len(kept_messages or []))
    if dropped <= 0:
        return ""
    activity = _collect_recent_tool_activity(original_messages, client_profile, limit=4)
    lines = [
        "<HISTORY COMPACTION NOTICE>",
        f"{dropped} older message(s) were compacted out of the inline history.",
        "The original goal and latest tool result in TASK MEMORY remain authoritative.",
    ]
    if activity:
        lines.append("Last known tool activity before/around compaction:")
        lines.extend(f"- {_clip_text(item, 260)}" for item in activity)
    lines.append("</HISTORY COMPACTION NOTICE>")
    return "\n".join(lines)


def build_prompt_with_tools(system_prompt: str, messages: list, tools: list, *, client_profile: str = OPENCLAW_OPENAI_PROFILE, workspace_root: str | None = None) -> str:
    # 闂傚倸鍊搁崐宄懊归崶顒婄稏濠㈣埖鍔曠粻姘舵倶閻愭彃鈷旀い鈺佸级缁绘繈妫冨☉鍗炲壈闂佽棄鍟伴崰鏍蓟濞戙垹唯妞ゆ梻鍘ч～鈺呮⒑缁嬭儻顫﹂柛鏃€鍨垮璇测槈閵忕姷鍔撮梺鍛婂姉閸嬫捇鎮鹃崼鏇熲拺闁兼亽鍎遍悘銉︺亜閿旂偓鏆€殿喖顭烽弫鎾绘偐閼碱剦妲版俊鐐€栭幐楣冨窗閹捐违闁归偊鍠氱壕钘壝归敐鍛儓闁告棑绠撻弻娑氣偓锝庡亝鐏忔澘菐閸パ嶈含闁诡喗鐟╅、鏃堝礋閵娿儰澹曞┑鐐村灟閸╁嫰寮繝鍌楁斀闁绘ɑ褰冮顏嗏偓瑙勬礀瀵爼骞堥妸銉庣喖宕归鎯у缚闂備胶顭堥鍌炲疾濠婂懏宕叉繛鎴欏灩楠炪垺淇婇姘倯闁革綆鍠氱槐鎾存媴閻熸澘顫嶉梺鎰佷簽椤ヮ柟tem 濠电姷鏁告慨鐑藉极閹间礁纾婚柣鎰惈閸ㄥ倿鏌ｉ姀鐘冲暈闁稿顑呴埞鎴︽偐閹绘帗娈?+ 婵犵數濮烽。钘壩ｉ崨鏉戠；闁规崘娉涚欢銈呂旈敐鍛殲闁稿顑嗘穱濠囧Χ閸屾矮澹曟俊?user 濠电姷鏁告慨鐑藉极閹间礁纾婚柣鎰惈閸ㄥ倿鏌ｉ姀鐘冲暈闁稿顑呴埞鎴︽偐閹绘帗娈銈嗘礋娴滃爼寮诲☉妯锋婵☆垰鍚嬮幉濂告⒑閸濆嫭濯奸柛鎾跺枛瀵鈽夐姀鈺傛櫇闂佹寧绻傚ú銊╂偩閻㈠憡鈷戦柛婵嗗閳ь剚鍨垮畷姗€鏁愰崱妯绘緫濠碉紕鍋戦崐鏍ь潖婵犳艾鐓曢柛顐犲劚绾惧潡骞栧ǎ顒€濡介柣鎾寸懄椤ㄣ儵鎮欓懠顑胯檸闂佽绻楃亸娆撳焵椤掑喚娼愭繛鍙夘焽閹广垽宕奸妷銉︽К闂侀潧顦弲娑橆啅濠靛洢浜滈柡宥冨妿閻倖淇? 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敂钘変罕闂佸憡鍔﹂崰鏍婵犳碍鐓欓柟瑙勫姦閸ゆ瑧绱?N 闂?
    # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁撻悩鍐蹭簻濡炪倖甯掗崐缁樼▔瀹ュ鐓欓弶鍫濆⒔閻ｉ亶鏌涢妸銉モ偓褰掑Φ閸曨垰鍐€妞ゎ厽鍨靛▓濂告⒑缂佹ɑ鈷掗柛妯犲洦鍊剁€规洖娲犻崑鎾舵喆閸曨剛顦ュ┑鐐跺皺婵炩偓鐎规洘鍨块獮妯肩磼濡厧寮抽梺璇插嚱缂嶅棝宕楀鈧鎼佸冀椤撶啿鎷洪梻鍌氱墛缁嬫挾绮婚崘娴嬫斀妞ゆ梹鍎抽。鑲╃磼閸屾氨校缂佽桨绮欏畷銊︾節閸曨偄绠炲┑鐘殿暯濡插懘宕归幎钘夊偍鐟滄柨顕ｉ崨濠冨劅妞ゎ偒鍏涚花璇差渻閵堝棗濮х紒鑼跺Г閹便劌顓兼径瀣幍濡炪倖鐗楅懝楣冾敂椤撶喆浜滈柕蹇ョ磿閹冲洭鏌熼鐓庘挃濞寸媴绠撻幃鍓т沪閼测晝顦ㄩ梻鍌氬€搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熸潏楣冩闁搞倖鍔栭妵鍕冀閵娧冩殹闂佽偐澧楃€笛囧Φ閸曨喚鐤€闁圭偓娼欏▍锝囩磽娴ｇ顣抽柛瀣仱楠炲牓濡搁妷顔藉缓闂佺硶鍓濋〃鍛偓娑崇秮濮?tool_use 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁撻悩鍐叉疄闂佸憡鎸嗛崱妞ワ繝姊洪崗鑲┿偞闁哄懏绮撻敐鐐哄即閵忥紕鍘藉┑掳鍊愰崑鎾绘煟濡も偓濡稑鈻庨姀銈嗗€烽柣鎴烆焽閸樺崬鈹戞幊閸婃洟宕锝囶浄婵犲﹤鎳愮壕濂告煟濡櫣浠涢柡鍡╁墴閺屸€崇暆鐎ｎ剛鐦堥悗瑙勬礃鐢帡鈥﹂妸鈺佺妞ゆ劧绲块弳姘舵⒒閸屾瑦绁版い鏇熺墵瀹曟澘螖閸愩劌鐏婇梺瑙勫礃椤曆囧几娴ｈ　鍋撻獮鍨姎妞わ富鍨堕弻瀣炊閵娧呯槇闂傚倸鐗婄粙鎺椝夐悙鐑樼厱濠电姴鍊块崣鍕叏婵犲啯銇濇鐐寸墵閹瑩骞撻幒婵堚偓铏繆閻愵亜鈧牠宕归棃娴㈡椽濡堕崼顫綍婵犵數鍋為幐濠氬春閸愵喖纾婚柟鍓х帛閻撴瑦銇勯弮鍥舵綈婵炲懎娲弻鐔风暋闁箑鍓堕悗瑙勬礈閸忔﹢銆佸鈧幃鈺呮濞戞艾鈧偤姊?"YES." 缂傚倸鍊搁崐鎼佸磹閹间礁纾归柣鎴ｅГ閸婂潡鏌ㄩ弴鐐测偓鍝ョ不娴煎瓨鍋ｉ柛銉戝嫧鏋欓梺缁樺笩婵倝濡甸崟顖氱疀闁割偅娲橀宥夋⒑?
    messages = list(messages or [])
    original_messages = list(messages)
    MAX_HISTORY_TURNS = 15  # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敂钘変罕闂佸憡鍔﹂崰鏍婵犳碍鐓欓柟瑙勫姦閸ゆ瑧绱?15 闂?= 30 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曚綅閸ヮ剦鏁冮柨鏇楀亾缂佲偓閸喓绡€闂傚牊绋撴晶銏ゆ煟椤撶喐宕岄柡宀嬬秮楠炲鏁愰崱鈺傤棄缂傚倷鑳舵慨鐢垫暜濡ゅ懎桅闁告洦鍨伴崘鈧梺闈涳工濞诧箑鐣濈粙璺ㄦ殾闁硅揪绠戠粻濠氭偣閸ヮ亜鐨洪柨娑欑矊閳规垿鎮欓弶鎴犱桓闂佸憡绻傞柊锝呯暦閹达附鏅濋柛灞剧〒閸樹粙姊虹紒妯荤叆闁硅姤绮撻獮濠囧礃椤旂晫鍘藉┑掳鍊愰崑鎾翠繆椤愶綆娈滈柛?闂傚倸鍊搁崐鎼佸磹閹间礁纾归柣銏㈩焾绾惧鏌熼崜褏甯涢柣鎾存礃閵囧嫰顢橀悢椋庝淮闂佸搫顑嗛悷褏妲愰幒妤€绠熼悗锝庡墰琚﹂梻浣告惈閺堫剛绮欓弽顐や笉婵炴垯鍨归崡鎶芥煟閹邦厼绲荤紒鐙呯秮濮婄粯鎷呮笟顖涙暞濠碘槅鍋勭€氱増淇婇崜浣虹煓閻犳亽鍔嶅▓楣冩⒑缂佹ê鐏卞┑顔哄€濆畷鎰磼濡湱绠氬銈嗙墬缁诲倿宕ラ崷顓熷枑闁哄鐏濈痪褏绱?5 闂傚倸鍊风粈渚€骞栭位鍥焼瀹ュ懐锛熼梺鍦濠㈡绮ｅΔ鍛厸闁搞儮鏅涘暩缂備胶濮甸弻銊┾€︾捄銊﹀磯濞撴凹鍨伴崜鎵磽娴ｇ顣抽柛瀣ㄥ€濆濠氭晲婢跺﹦鐫勯梺绋挎湰閼圭偓淇婂ú顏呪拺闁告繂瀚ˉ婊呯磼缂佹﹫鑰跨€殿喖顭锋俊鎼佸Ψ閵忊槅娼旀繝纰樻閸垳鎷冮敃鈧埢鎾活敇閻樼數锛?婵犵數濮烽弫鍛婃叏娴兼潙鍨傞柣鎾崇岸閺嬫牗绻涢幋鐑嗙劷闁哄棴闄勯妵鍕箳閹存績鍋撻悷鎵殾闁哄被鍎查悡鏇犫偓鍏夊亾闁逞屽墴瀹曟垿鎮欓悜妯轰簵闂佺鏈竟鏇㈠磻閹捐崵宓侀柛顭戝枛婵骸鈹戦埥鍡椾簼闁荤啿鏅涢～?
    if tools and client_profile == CLAUDE_CODE_OPENAI_PROFILE and len(messages) > MAX_HISTORY_TURNS * 2:
        system_messages = [m for m in messages if m.get('role') == 'system']
        # 闂傚倸鍊搁崐椋庣矆娴ｉ潻鑰块梺顒€绉撮弸渚€鏌ゆ慨鎰偓妤佺▔瀹ュ鐓涚€广儱楠搁獮鎴︽煃瑜滈崜娆撳箠韫囨洘宕叉繝闈涙－濞尖晜銇勯幒鎴濃偓鍧楁偘閹剧粯鈷掑ù锝堫潐閻忛亶鏌￠崨顔炬创鐎规洦鍨堕、娑橆煥閸涱剛鐟濋梻浣告贡閸庛倝銆冮崱娑樼９闁汇垹鎲￠悡鏇㈡煥閺冨浂鍤欐鐐寸墪闇夐柣妯绘そ閸濆搫菐閸パ嶅姛缂佽鲸甯℃慨鈧柣妯哄悁濡楁捇鏌ｆ惔銏╁晱闁革綆鍣ｅ畷婊堟偄妞嬪孩娈鹃梺鍓插亝濞叉牗鍎梻浣瑰濡礁螞閸曨垼鏁傚ù鐓庣摠閳锋帒霉閿濆懏鍟為柛鐔哄仱閺屾盯寮埀顒€煤閻旇偐宓佸┑鐘插暞閸庣喐銇勯鐔风仸闁哄應鏅犲铏规喆閸曨偒妫嗘繝鈷€鍕垫疁鐎?user 濠电姷鏁告慨鐑藉极閹间礁纾婚柣鎰惈閸ㄥ倿鏌ｉ姀鐘冲暈闁稿顑呴埞鎴︽偐閹绘帗娈銈嗘礋娴滃爼寮诲☉妯锋婵☆垰鍚嬮幉濂告⒑閸濆嫭濯奸柛鎾跺枛瀵鈽夐姀鈺傛櫇闂佹寧绻傚ú銊╂偩閻㈠憡鈷戦柛婵嗗閳ь剚鍨垮畷姗€鏁愰崱妯绘緫濠碉紕鍋戦崐鏍ь潖婵犳艾鐓曢柛顐犲劚绾惧潡骞栧ǎ顒€濡介柣鎾寸懄椤ㄣ儵鎮欓懠顑胯檸闂佽绻楃亸娆撳焵椤掑喚娼愭繛鍙夘焽閹广垽宕煎┑鎰偓鍫曟⒑椤掆偓缁夌敻鎮″▎鎾寸厽鐟滃秹骞楀鍛煋妞ゆ洍鍋撻柡宀€鍠栭、娆撴偩鐏炴儳娅氶梻浣筋嚃閸ㄤ即宕愰崹顔炬殾妞ゆ劧绠戝敮閻熸粍绮屽嵄闁圭虎鍠楅埛鎺懨归敐鍥ㄥ殌妞ゆ洘绮庣槐鎺斺偓锝庡亜閻忔挳鏌熷畷鍥ф灈妞ゃ垺鐩幃娆撳级閹存粍鍋呴梻鍌欒兌缁垶鈥﹂崼婵冩瀺闁挎繂顦伴崑顏堟煃瑜滈崜鐔奉潖濞差亜宸濆┑鐘插婵洭姊烘导娆戠У濞存粠浜畷娲焵椤掍降浜滈柟鍝勬娴滃墽绱撴担铏瑰笡闁搞劌婀遍崚鎺戭潩鐠鸿櫣顢呴梺缁樺姀閺呮粓寮埀顒勬⒒娴ｇ顥忛柛瀣瀹曚即骞囬悧鍫濅患?
        first_user = next(
            (m for m in messages
             if m.get('role') == 'user'
             and _extract_user_text_only(m.get('content', ''), client_profile=client_profile).strip()),
            None,
        )
        recent_messages = messages[-(MAX_HISTORY_TURNS * 2):]
        # 婵犵數濮烽弫鍛婃叏閻戝鈧倹绂掔€ｎ亞鍔﹀銈嗗坊閸嬫捇鏌涢悢閿嬪仴闁糕斁鍋撳銈嗗坊閸嬫挾绱撳鍜冭含妤犵偛鍟灒閻犲洩灏欑粣鐐烘煟鎼搭垳鍒板褍娴锋竟鏇熴偅閸愨斁鎷洪梺鍛婄箓鐎氼喛鈪归梻浣告啞閺屻劎绮旇ぐ鎺戠?user 闂傚倷娴囬褍顫濋敃鍌︾稏濠㈣埖鍔栭崑銈夋煛閸モ晛小闁绘帒锕ョ换娑㈠幢濡櫣浠撮梺鎼炲妽缁诲牓寮婚妸鈺傚亜闁告繂瀚呴姀銈嗙厵?recent 闂傚倸鍊搁崐鎼佸磹閻戣姤鍊块柨鏇氶檷娴滃綊鏌涢幇闈涙灍闁搞倖鍔栭妵鍕冀閵娿儱姣堝┑鐐茬毞閺呯娀寮婚弴鐔虹闁绘劦鍓氶悵鏃傜磽娴ｆ彃浜鹃梺绯曞墲鑿уù婊勭矒閺岀喖寮堕崹顕呮殺闂佷紮缍€妞村摜鎹㈠☉銏犲窛妞ゆ劑鍨绘禒鐓庮渻?
        if first_user is not None and first_user not in recent_messages:
            messages = system_messages + [first_user] + recent_messages
            log.info(f"[Prompt] trimmed history with system+original user+last {MAX_HISTORY_TURNS} turns (messages={len(messages)})")
        else:
            messages = system_messages + recent_messages
            log.info(f"[Prompt] trimmed history with system+last {MAX_HISTORY_TURNS} turns (messages={len(messages)})")

    MAX_CHARS = 40000 if tools else 120000
    sys_part = _build_system_prompt_block(system_prompt, tools, client_profile)
    tools_part = _build_tool_instruction_block(tools, client_profile) if tools else ""
    workspace_notice = build_workspace_notice(workspace_root) if tools and client_profile == CLAUDE_CODE_OPENAI_PROFILE else ""
    task_memory_part = _build_task_memory_block(messages, tools, client_profile)
    max_history_msgs = _history_window_limit(tools, client_profile)
    history_window_messages = messages[-max_history_msgs:] if tools and len(messages) > max_history_msgs else messages
    dropped_history_part = _build_dropped_history_summary(original_messages, history_window_messages, tools, client_profile)

    overhead = len(sys_part) + len(tools_part) + len(workspace_notice) + len(task_memory_part) + len(dropped_history_part) + 50
    budget = MAX_CHARS - overhead
    history_parts = []
    used = 0
    NEEDSREVIEW_MARKERS = ("needs-review", "recap", "summary", "code review", "review findings", "[needs-review]", "**needs-review**")
    msg_count = 0
    for msg in reversed(messages):
        if msg_count >= max_history_msgs:
            break
        role = msg.get("role", "")
        if role not in ("user", "assistant", "system", "tool"):
            continue
        if role == "system" and system_prompt and _extract_text(msg.get("content", ""), client_profile=client_profile).strip() == system_prompt.strip():
            continue

        if role == "tool":
            tool_content = msg.get("content", "") or ""
            tool_call_id = msg.get("tool_call_id", "")
            if isinstance(tool_content, list):
                tool_content = "\n".join(
                    p.get("text", "") for p in tool_content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            elif not isinstance(tool_content, str):
                tool_content = str(tool_content)
            tool_result_limit = 6000 if (client_profile == CLAUDE_CODE_OPENAI_PROFILE and tools) else 300
            #if len(tool_content) > tool_result_limit:
            #    tool_content = tool_content[:tool_result_limit] + "...[truncated]"
            line = f"[Tool Result]{(' id=' + tool_call_id) if tool_call_id else ''}\n{tool_content}\n[/Tool Result]"
            if used + len(line) + 2 > budget and history_parts:
                break
            history_parts.insert(0, line)
            used += len(line) + 2
            msg_count += 1
            continue

        user_text_only = _extract_user_text_only(msg.get("content", ""), client_profile=client_profile) if role == "user" else ""
        text = _extract_text(
            msg.get("content", ""),
            user_tool_mode=(bool(tools) and role == "user" and client_profile == CLAUDE_CODE_OPENAI_PROFILE),
            client_profile=client_profile,
        )

        if role == "assistant" and not text and msg.get("tool_calls"):
            tc_parts = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args_str = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, ValueError):
                    args = {"raw": args_str}
                tc_parts.append(_render_history_tool_call(name, args, client_profile))
            text = "\n".join(tc_parts)

        if tools and role == "assistant" and any(m in text for m in NEEDSREVIEW_MARKERS):
            log.debug(f"[Prompt] skipped assistant needs-review marker message (chars={len(text)})")
            msg_count += 1
            continue
        lower_text = text.lower()
        is_tool_result = role == "user" and (
            "[tool result" in lower_text
            or text.startswith("{")
            or "\"results\"" in text[:100]
        )
        if client_profile == CLAUDE_CODE_OPENAI_PROFILE and tools:
            if is_tool_result:
                max_len = 6000
            elif role == "assistant":
                max_len = 500
            else:
                max_len = max(1600, budget - used - len("Human: ") - 2)
        else:
            max_len = 600 if is_tool_result else max(1400, budget - used - len("Human: ") - 2)
        #if len(text) > max_len:
        #    text = text[:max_len] + "...[truncated]"
        is_tool_result_only_user_msg = role == "user" and not user_text_only.strip() and bool(text.strip())
        prefix = "" if is_tool_result_only_user_msg else {"user": "Human: ", "assistant": "Assistant: ", "system": "System: "}.get(role, "")
        line = text if is_tool_result_only_user_msg else f"{prefix}{text}"
        if used + len(line) + 2 > budget and history_parts:
            break
        history_parts.insert(0, line)
        used += len(line) + 2
        msg_count += 1

    # 闂傚倸鍊搁崐宄懊归崶顒夋晪闁哄稁鍘奸崒銊ф喐閻楀牆绗掗柛銊ュ€婚幉鎼佹偋閸繂鎯為梺鎼炲労閸撴瑩鎷戦悢鍏肩厪濠㈣泛鐗嗛崝姘舵煕鐎ｎ亞效婵﹨娅ｇ槐鎺懳熼懡銈呭汲婵＄偑鍊戦崝灞绢殽閹间讲鈧箓宕堕‖顒佹閸┾偓妞ゆ帒瀚粻鎺楁⒒娴ｈ櫣甯涢惇澶愭偣閳ь剟鏁冮崒姘優濠电偛妫楃换鍡涘绩閼恒儯浜滈柡鍐ｅ亾闁稿孩濞婂鎼佸箣閻樼數锛滈柣搴秵娴滆泛危閸︻厾纾肩紓浣诡焽缁犳牠鏌熷畡鐗堝殗闁圭厧婀遍幉鎾礋椤愶絾顔掗梻鍌氬€搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鍦磼鐎ｎ偓绱╃憸鐗堝笒缁€瀣亜閹捐泛娅忔繛鑲╁枛濮婅櫣绱掑鍡樼暥闂佺粯顨堥崑鐔肺ｉ幇鏉跨睄闁割偆鍠撻崢鎼佹倵楠炲灝鍔氶柛鐕佸亝娣囧﹥绂掔€ｎ偆鍙冮梺绋挎湰閸戝綊宕曢弮鍫熺厸鐎光偓鐎ｎ剛鐦堥悗瑙勬礈閸樠囧煘閹达箑绠涙い鎾愁檧缁犳挸顫忛崫鍕懷囧炊瑜夐崑鎾诲即閻橆偄浜炬慨姗嗗幗缁跺弶銇勯銏㈢缂佸倹甯為埀顒婄秵娴滄粎绮ｅ☉姗嗘富闁靛牆妫欓埛鎺楁煛閸滀礁浜伴柛鈹惧亾濡炪倖甯掗ˇ顖氣枍瀹ュ鐓涚€光偓鐎ｎ剛鐦堥悗瑙勬礀閵堟悂銆侀弴銏狀潊闁绘瑢鍋撴繛鑲╁亾缁?user 濠电姷鏁告慨鐑藉极閹间礁纾婚柣鎰惈閸ㄥ倿鏌ｉ姀鐘冲暈闁稿顑呴埞鎴︽偐閹绘帗娈銈嗘礋娴滃爼寮诲☉妯锋婵☆垰鍚嬮幉濂告⒑閸濆嫭濯奸柛鎾寸洴閸┾偓妞ゆ巻鍋撻柛妯荤矒瀹曟垿骞樼紒妯煎幈闂佸搫娲㈤崝瀣姳閻ｅ瞼纾奸弶鍫涘妼閸濈儤鎱ㄦ繝鍛仩缂侇喗鐟ч幑鍕Ω瑜滈崬鍓佺磽閸屾瑧顦︽い锔垮嵆楠炴劗鈧湱濮甸ˉ銈夋⒒娴ｇ顥忛柣鎾崇墦瀹曟娊顢欑喊杈╁姺?tool_use 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁撻悩鍐叉疄闂佸憡鎸嗛埀顒勫磻閹炬枼妲堥柟鐑樻尰閻濇姊洪崫銉ユ珡闁搞劏娉涢～蹇撁洪鍕啇闂佺粯鍔栬ぐ鍐€栭崼婵愭富闁靛牆妫楅悘銉╂倵濮樼厧澧撮柛鈹垮劜瀵板嫰骞囬崹顐ｆ珕闂備礁澹婇崑鍡涘窗鎼淬劌绀堟い鏃傗拡濞撳鏌曢崼婵囶棡闁抽攱甯￠弻锟犲椽娴ｉ晲鍠婇悗瑙勬礉椤缂撴禒瀣窛濠电姴瀚敮?
    # 闂傚倸鍊峰ù鍥敋瑜忛懞閬嶆嚃閳轰胶绛忔繝鐢靛У閻旑剛绱為弽褜鐔嗛悹杞拌閸庢劖绻涢崨顖毿ラ柍褜鍓欑粻宥夊磿闁单鍥敍濠ф儳浜?profile 闂傚倸鍊搁崐鎼佸磹妞嬪孩顐介柨鐔哄Т缁愭鏌￠崶鈺佇㈤柛銊︾箖缁绘盯宕卞Ο璇茬闂佺粯甯掗悘姘跺Φ閸曨垰绠抽柛鈩冦仦婢规洟姊绘担渚劸闁挎洏鍊濋垾锕€鐣￠柇锕€娈ㄥ銈嗗姧缁犳垵娲垮┑鐘灱濞夋盯顢栭崱妞绘瀺闁哄稁鍘介埛鎴︽煕閹炬潙绲诲ù婊勫姍閺岀喓绮甸崷顓犘滈梺绯曟櫔缁绘繂鐣烽妸鈺婃晩閻熸瑥瀚惁閬嶆⒒閸屾瑧绐旀繛浣冲洦鍋嬮柛鈩冪☉缁犵娀骞栧ǎ顒€濡兼俊顐ｎ焽閹叉悂寮崼婵婃憰濠电偞鍨剁划搴ㄦ偪閳ь剟姊虹憴鍕婵炲鐩妴鍌炲传閸曞灚瀵?Claude Code 闂傚倸鍊搁崐鐑芥倿閿曞倹鍎戠憸鐗堝笒缁€澶屸偓鍏夊亾闁逞屽墴閸┾偓妞ゆ帊绀侀崵顒勬煕濮椻偓缁犳牕顕ｉ锔绘晪闁逞屽墴閻涱喖螣閼测晝锛滃┑鈽嗗灦閺€杈┾偓鍨墵濮婄粯绗熼埀顒€顭囪閹囧幢濞嗘劕搴婇梺鍦劋濮婂鎯岄崱娑欑厱闁斥晛鍟伴埊鏇㈡煟?
    if tools and messages:
        first_user = next(
            (
                m for m in messages
                if m.get("role") == "user"
                and _extract_user_text_only(m.get("content", ""), client_profile=client_profile).strip()
            ),
            None,
        )
        if first_user:
            first_text = _extract_user_text_only(first_user.get("content", ""), client_profile=client_profile)
            first_short = first_text #[:800] + ("...[original task truncated]" if len(first_text) > 800 else "")
            first_line = f"Human (ORIGINAL TASK): {first_short}" if client_profile == CLAUDE_CODE_OPENAI_PROFILE else f"Human: {first_short}"
            if not history_parts or not history_parts[0].startswith(f"Human: {first_text[:60]}") and not history_parts[0].startswith(f"Human (ORIGINAL TASK): {first_text[:60]}"):
                first_line_cost = len(first_line) + 2
                if first_line_cost <= budget:
                    while history_parts and used + first_line_cost > budget:
                        removed = history_parts.pop()
                        used -= len(removed) + 2
                    history_parts.insert(0, first_line)
                    used += first_line_cost
                    log.info(f"[Prompt] injected original task summary into history (chars={len(first_short)})")


    latest_user_line = ""
    latest_user = None
    latest_text = ""
    if tools and messages:
        latest_user = next(
            (
                m for m in reversed(messages)
                if m.get("role") == "user"
                and _extract_user_text_only(m.get("content", ""), client_profile=client_profile).strip()
            ),
            None,
        )
    latest_is_tool_result = _latest_message_is_tool_result(messages, client_profile) if tools else False
    if latest_user and not latest_is_tool_result:
        latest_text = _extract_user_text_only(latest_user.get("content", ""), client_profile=client_profile).strip()
        if latest_text:
            latest_budget = max(900, budget - used - len("Human (CURRENT TASK - TOP PRIORITY): ") - 2)
            latest_short = latest_text[:latest_budget] + ("...[latest task truncated]" if len(latest_text) > latest_budget else "")
            latest_user_line = f"Human (CURRENT TASK - TOP PRIORITY): {latest_short}"

    latest_user_is_tool_related = _looks_tool_related(latest_text)


    if tools and log.isEnabledFor(logging.DEBUG):
        tool_names = [tool.get("name", "") for tool in tools if tool.get("name")]
        tool_instruction_preview = _safe_preview(tools_part, 360)
        latest_user_preview = _safe_preview(latest_user_line, 220)
        first_user_preview = ""
        if messages:
            first_user = next((m for m in messages if m.get("role") == "user"), None)
            if first_user:
                first_user_preview = _safe_preview(
                    _extract_text(
                        first_user.get("content", ""),
                        user_tool_mode=(client_profile == CLAUDE_CODE_OPENAI_PROFILE),
                        client_profile=client_profile,
                    ),
                    220,
                )
        log.debug(
            "[Prompt] history summary: history_msgs=%s history_chars=%s tool_count=%s tool_names=%s first_user=%r latest_user=%r tool_instr=%r",
            len(history_parts),
            used,
            len(tool_names),
            tool_names[:12],
            first_user_preview,
            latest_user_preview,
            tool_instruction_preview,
        )
    # 缂傚倸鍊搁崐鎼佸磹閹间礁纾瑰瀣捣閻棗霉閿濆牄鈧偓闁稿鎸搁～婵嬫偂鎼达絼妗撻梺鑺ヮ焽閸犲酣鍩為幋锔藉亹闁圭粯甯楀▓褎绻涚€涙鐭嬬紒顔芥崌瀵鈽夊Ο閿嬫杸闂佺硶鍓濋〃蹇斿閳ь剛绱撴担鍝勪壕婵犮垺顭囩划鏃囥亹閹烘垼鎽曢梺闈涱焾閸庮喖危閸喍绻嗘い鏍ㄧ矊鐢埖銇勯敐鍡樸仢婵﹥妞藉畷銊︾節鎼存繄绌块梻浣规偠閸庮垶宕濇惔銊ョ煑闁糕剝顨忓〒濠氭煏閸繃顥炵紒鈧埀顒€鈹戦埥鍡椾簼缂佸鎸鹃崚鎺楀醇閵夈儱鑰垮┑鐐村灦閻熝囧矗?
    #   [sys_part]
    #   [tools_part]           闂傚倷娴囬褍顫濋敃鍌︾稏濠㈣埖鍔曠粻鏍煕椤愶絾绀€缁炬儳娼″娲敆閳ь剛绮旈幘顔藉剹婵°倕鎳忛悡鏇犳喐鎼淬劊鈧啴宕ㄩ婊€绗夋繛瀵稿帶閻°劑鎮￠弴銏＄厪濠㈣埖锚閻忥附淇婄紒銏犳灈闁宠鍨块、娆撳传閸曨厺绱欓梻浣告惈閺堫剛绮欓弽顓勫洭鎼归鐘辩盎闂侀潧顭粻鎴炴叏婢跺绻嗛柛娆忣槸婵洦銇勯鈧敃顏勭暦閹惧椋庡姬缁虹尣 marker 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁撻悩顔瑰亾閸愵喖宸濇い鏍ㄧ☉鎼村﹤鈹戞幊閸婃洟骞忕€ｎ喖鏋佸┑鐘叉处閻撴洘绻涢幋鐑嗙劷闁圭晫濞€閺?
    #   [few-shot]             婵犵數濮烽弫鍛婃叏娴兼潙鍨傚┑鍌滎焾閺勩儵鏌″畵顔兼湰閸嶇敻姊洪棃娴ゆ盯宕熼锛勭梾濠碉紕鍋戦崐鏍礉瑜忕划濠氬箣閻樼數鐒奸梺绯曞墲鑿уù婊勭矒閺岀喖寮剁捄銊ょ驳缂備浇鍩栧銊ф閹烘绠熼悗锝庡墰琚﹂梻浣告惈閺堫剛绮欓弽顐や笉婵炴垶菤濡插牓寮堕崼顐ゅ帥婵☆偅鐗犲缁樼瑹閳ь剙顭囪鐓ら柨鏇楀亾闁伙絾绻堥獮鏍ㄦ媴閸濄儳鏋冮梻浣虹帛椤牓顢氳缁牓宕橀钘変化闂佽鍘界敮鎺撲繆閹稿簺浜滈柨鏃囶潐濞呭﹪鏌″畝瀣М闁诡喒鏅滃蹇涱敃椤愩垺鏆梻鍌欑閹碱偊骞婅箛鏇炲灊闊洦绋戦悿顔姐亜閺嶎偄浠﹂柍閿嬪灴閺岀喖鎳栭埡浣风捕婵犵鈧啿鎮戦柕鍥у楠炲鈹戦崶褎鐣绘俊鐐€戦崹娲€冩繝鍥х畺闁靛繈鍨婚惌娆愮箾閸℃ê鍔ゆ繛鍫濆悑缁绘繂鈻撻崹顔界亶闂佹寧姘ㄩ惀顏嗙磼閵忕姴绠归梺?MCP闂?
    #   [history_parts]        闂傚倸鍊搁崐鐑芥倿閿曗偓椤啴骞愭惔锝庢锤闂佺粯鍔曢幖顐ょ玻濡ゅ懎绠规繛锝庡墮婵′粙鏌涚€ｅ吀閭柡灞剧洴瀵挳濡搁妷銉ь唶闂備胶顭堥鍡涘箲閸ヮ剙绠栭柦妯侯槴閺嬫棃鏌熺粙鍨劉闁哄棛鍋ゅ?+ tool_use / tool_result闂傚倸鍊搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熸潏楣冩闁稿顑夐悡顐﹀炊閵娧€妲堢紓浣哄Х閺佸寮婚妸銉㈡斀闁糕檧鏅滄晥闂?Assistant: 婵犵數濮烽弫鎼佸磻濞戙埄鏁嬫い鎾跺枑閸欏繐霉閸忓吋缍戠痪鎯ф健閺岀喎鈻撻崹顔界亾闂佹椿鍘藉畝鎼佸箖鐟欏嫭濮滈柟娈垮枤鍗忛梻浣虹帛濮婂宕㈣閹ê鐣烽崶锝呬壕閻熸瑥瀚粈鈧┑鐐跺皺婵炩偓鐎规洘鍨块獮妯肩磼濡　鍋撴繝姘參婵☆垯璀﹀Σ娲煟閵堝倸浜鹃梻鍌氬€烽懗鍓佸垝椤栨娑欐媴缁洘鐎洪梺鎸庣箓椤︻垶鎯屽Δ鍛厓鐟滄粓宕滈悢濂夋綎?
    #   [latest_user_line]     闂傚倷娴囧畷鐢稿窗閹邦喖鍨濋幖娣灪濞呯姵淇婇妶鍛櫣缂佺姳鍗抽弻娑樷槈濮楀牊鏁惧┑鐐叉噽婵炩偓闁哄矉绲借灒闁惧繒娅㈢槐鐐差渻閵堝倹娅囬柛蹇旓耿楠炲啳銇愰幒鎴犲€為梺闈涱焾閸庡磭绮婂畡閭︽富?
    #   Assistant:
    #
    # 闂傚倸鍊搁崐鎼佸磹閻戣姤鍊块柨鏇氶檷娴滃綊鏌涢幇鍏哥敖闁活厽鎹囬弻锝夊箣濠垫劖缍楅梺閫炲苯澧柟铏耿瀵偊宕橀鑲╋紲濠电偞鍨堕悷锕傛偟椤愶絿绡€婵炲牆鐏濋弸鐔搞亜椤撶偟澧涢柕鍥ㄦ楠炴牗鎷呴崨濠冃氶梻浣侯焾閻ジ宕戦悙鍝勭９闁汇垹鎲￠悡鏇㈡煥閺冨浂鍤欐鐐寸墪闇夐柣妯虹－閻﹪妫佹径鎰厱闊洦娲栫敮璺衡攽椤旇偐肖闁逞屽墲椤煤濮椻偓閵嗗啴宕ㄧ€涙ê浠奸梺璺ㄥ枔婵鐥閺屾盯鈥﹂幋婵囩亶缂傚倸绉撮ˇ闈涱潖缂佹ɑ濯撮柛娑橈工閺嗗牓姊洪懡銈呮珢缂佺姵甯℃俊鐢稿箛閺夎法顦ㄥ銈呯箰閹冲孩鎯旀繝鍥ㄢ拺闂侇偆鍋涢懟顖涙櫠椤斿浜滄い蹇撳閺嗭絽鈹戦垾宕囧煟鐎规洖鐖奸崺鈩冩媴娓氼垰浠归梻鍌欑劍閻綊宕洪崟顖氬瀭闂侇剙绉寸粻鏌ユ煕閵夋垵鎳忓▓?few-shot 婵犵數濮烽弫鎼佸磻閻愬搫鍨傞柛顐ｆ礀閽冪喖鏌曟繛鐐珦闁轰礁瀚伴弻娑樷槈濞嗘劗绋囩紓浣哄У閻楁绌辨繝鍥ч柛娑卞枛濞咃綁寮堕埡鍌滅畺缂佺粯鐩獮瀣倷閺夋垹顣插┑鐘媰閸曨剦鈧常stant: 婵犵數濮烽弫鎼佸磻閻愬搫鍨傞柛顐ｆ礀閽冪喖鏌曟繛鐐珦闁轰礁瀚伴弻娑樷槈閸楃偞鐏嶅┑鐐叉噽婵炩偓闁哄矉绲借灒闁兼祴鏅涚粭锟犳⒑?
    # 闂傚倸鍊搁崐鐑芥倿閿曞倸绠栭柛顐ｆ礀绾惧潡鏌ц箛锝呬簼闁告瑥绻掗埀顒冾潐濞叉牕煤閿旈敮鍋撳顒夌吋闁哄矉缍佸顕€宕惰濡叉劙姊虹紒妯烩拻闁告鍥ㄥ€剁€规洖娲犻崑鎾舵喆閸曨剛顦ュ┑鐐跺皺婵炩偓鐎规洘鍨块獮妯肩磼濡　鍋撴繝姘厾闁诡厽甯掗崝姘舵煕閹垮啫寮慨濠冩そ瀹曘劍绻濋崘顭戞П闂備礁鎲￠幐璇茬暆缁嬫鍤曢柟鎯版闁卞洭鏌曟径娑橆洭闁告鏁诲娲传閸曞灚歇濠电偛顦板ú婊呭垝婵犳艾绾ч幖瀛樻尰閺傗偓婵＄偑鍊栧Λ渚€宕戦幇顔句笉闁煎鍊愰崑鎾舵喆閸曨剛顦ㄩ梺鎼炲姀閸嬫劗鍒掔拠娴嬫闁靛繒濮烽鎺楁⒑閸濆嫷妲归柛銊у枛瀹?prompt 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敂钘変罕濠电姴锕ら悧鍡欏婵犳碍鐓曢柡鍥ュ妼閻忥繝鏌涚€ｎ亜顏柕鍥у楠炴帡骞嬪┑鎰偅闁哄鏅滅换鍫濐潖閾忓湱鐭欓柟绋垮閹疯京绱撴担绛嬪殭闁稿﹤娼￠妴?history 闂傚倸鍊风粈渚€骞栭位鍥敃閿曗偓閻ょ偓绻濇繝鍌涘櫣闁搞劍绻堥獮鏍庨鈧俊鐑芥煃瑜滈崜姘舵偋閻樿尙鏆﹂柛顐ｆ处閺佸棝鏌嶈閸撴盯鍩€椤掍浇澹樻い锔垮嵆婵＄敻宕熼姘辩杸闂佸疇妗ㄩ懗鑸靛閸曨垱鈷戦柛婵勫劚閺嬫垿鏌ｉ幙鍕瘈鐎殿喖顭锋俊鎼佸Ψ閵忊槅娼旀繝纰樻閸ㄥ磭鍒掗鐐茬闂侇剙绉甸悡鐘绘煕閵婏妇鈯曟繛鍛躬閺岋紕浠﹂崜褎鍒涙繝纰樺墲閹倹淇婇悜绛嬫晩闁绘挸楠搁ˉ宥夋⒑閼姐倕鏋戠紒顔肩У娣囧﹪骞栨担绋跨獩濡炪倖姊婚埛鍫ュ焵椤掍胶娲存慨濠冩そ瀹曨偊宕熼崹顐嶎亜鈹戦悙宸Ч婵炲弶绮撻獮?few-shot 缂傚倸鍊搁崐鎼佸磹妞嬪孩濯奸柡灞诲劚绾惧鏌熼悙顒傜獮闁哄啫鐗婇弲婵嬫煃瑜滈崜鐔煎箖濡　鏀介悗锝庡亜閸撱劑姊绘笟鍥у缂佸鏁婚幃?
    # 闂傚倸鍊峰ù鍥敋瑜忛幑銏ゅ箛椤旇棄搴婇梺褰掑亰閸剚绂嶉悷閭︾唵閻犺櫣鍎ゅ﹢鐗堛亜椤愶絾绀€闂囧鏌涜箛鎾虫倯缂傚秵鍨块弻锝夘敇閻曚焦鐣奸梺閫炲苯澧紒鐘茬Ч瀹曟洟鏌嗗鍛唵闁诲函缍嗛埀顒夊弿缁插墽鎹㈠┑鍡╂僵妞ゆ挻绋掔€氬ジ姊绘担鍛婅础缂侇噮鍨抽弫顕€鎮欓懠顒佹噧婵?few-shot 闂傚倸鍊搁崐鎼佸磹閻戣姤鍊块柨鏇氶檷娴滃綊鏌涢幇闈涙灍闁稿孩顨婇弻娑樼暆閳ь剟宕戝☉姘棜濠电姵纰嶉悡鏇熺箾閹存繂鑸归柡瀣ㄥ€濋弻宥囩磼濡儵鎷归梺闈涙搐鐎氼垳绮诲☉銏犵闁归妞掓潻妯肩磽閸屾瑧鍔嶉柛鐐跺吹缁辩偞绗熼埀顒€顕ｇ拠娴嬫婵犲﹤鎳愰弶鎼佹⒑鐟欏嫬绀冩繛鍛礋椤㈡瑩鍩€椤掑倻纾介柛灞剧懄缁佹澘顪冪€涙ɑ鍊愰柟顔惧厴閸┾剝鎷呴悜妯活啎婵犲痉鏉库偓鏇㈠疮娴煎瓨鍎楁繛鍡樻尰閸嬶綁鏌熼鐔风瑨濠碉紕鍏橀弻娑氣偓锝庡亝瀹曞矂鏌熼鐣岀煉闁瑰磭鍋ゆ俊鐑藉Ψ閵夈儮鎷婚梻鍌氬€烽悞锕傚箖閸洖纾挎い鏇楀亾鐎殿喗褰冮埥澶婎潩鏉堛劌娅橀梻鍌氬€搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熼悜姗嗘闁轰礁妫濋弻宥嗙瑹椤栨稒鍤恄page 闂傚倸鍊峰ù鍥х暦閻㈢纾婚柣鎰惈缁€鍕喐閻楀牆绗掔痪鎯ь煼閺屾稑鈽夐崡鐐茬濠电偞鍨崹鐟版纯濠电姰鍨煎▔娑㈩敄閸岀偛绠伴柛鎰靛枟閳锋垹鎲搁悧鍫濈瑨濞存粈鍗抽弻娑㈠箻閺夋垵鎽甸悗瑙勬礃濞茬喎顕ｉ幘顔碱潊闁挎稑瀚獮妤佺節閻㈤潧孝闁挎洏鍊栭〃銉╁箹娴ｇ懓鈧爼鏌曟径鍡樻珕闁绘挾鍠栭弻鏇熺箾瑜嶉崯顖炴倶閸儲鈷戦悹鍥ｂ偓铏亞缂備緡鍠楅悷鈺呭垂妤ｅ啯鏅濋柛灞炬皑椤斿﹪姊洪崫鍕殭闁稿妫楀嵄闁圭虎鍠楅埛?
    parts = []
    if sys_part:
        parts.append(sys_part)
    if tools_part:
        parts.append(tools_part)
    if workspace_notice:
        parts.append(workspace_notice)
    if task_memory_part:
        parts.append(task_memory_part)
    if dropped_history_part:
        parts.append(dropped_history_part)

    # Namespace-based few-shot闂傚倸鍊搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熼悜妯诲暗闁崇懓绉电换娑橆啅椤旂粯鍠氶梺杞扮閿曨亪寮诲鍫闂佸憡鎸荤喊宥囩矚鏉堛劎绡€闁搞儴鍩栭弲顒€鈹戦敍鍕哗妞ゆ泦鍕洸闁告挆鈧崑鎾舵喆閸曨剛顦ュ┑鐐跺皺婵炩偓鐎规洘鍨块獮姗€骞栭鐔溠囨煙閻撳海鎽犻柨姘瑰鍛壕缂佺粯鐩獮瀣攽閸剛绀婄紓鍌欐祰妞村憡绔熼崱娆愵潟闁圭儤鎸荤紞鍥煏婵炲灝鍔存俊顐㈡濮婃椽鎮烽柇锔界枃闂佺顑呴敃銈夋偩瀹勯偊娼ㄩ柍褜鍓熼妴渚€寮崼婵嗙獩濡炪倖姊婚悺鏃堝触閸岀偞鈷掗柛灞剧懅椤︼附绻濋埀顒勬焼瀹ュ啠鍋撻崒娑氼浄閻庯綆浜為敍娑㈡⒑閻熸澘鈷旂紒顕呭灦閹€斥槈閵忥紕鍘卞銈嗗姧缁茶法绮婚妷锔跨箚闁告瑥顦伴妵婵嬫煙椤旀寧纭炬い顐ｇ箞閹剝鎯斿┑鍡樼€抽梺璇叉唉椤煤濡櫣鏆嗛柟闂撮檷閳ь兛绶氬鎾綖椤斿墽鈼ゆ俊鐐€栭幐鐐叏閻戣姤鍋傞柟杈鹃檮閳?
    few_shot_chars = 0
    if tools and client_profile == CLAUDE_CODE_OPENAI_PROFILE and latest_user_is_tool_related:
        few_shot_tools = pick_few_shot_tools(tools, max_third_party=2)
        if len(few_shot_tools) >= 2:
            def _render_tc(name: str, input_data: dict) -> str:
                return render_qnml_tool_call(to_qwen_name(name), input_data)
            user_fs, asst_fs = render_few_shot_turn(few_shot_tools, _render_tc, thinking_enabled=False)
            few_user = f"Human: {user_fs}"
            few_asst = f"Assistant: {asst_fs}"
            parts.append(few_user)
            parts.append(few_asst)
            few_shot_chars = len(few_user) + len(few_asst) + 4
            log.info(f"[FewShot] injected={len(few_shot_tools)} tools={tool_summary_for_log(few_shot_tools)}")
    elif tools and client_profile == CLAUDE_CODE_OPENAI_PROFILE:
        log.debug("[FewShot] skipped: no representative tool examples selected")

    # 闂傚倸鍊搁崐鐑芥倿閿曗偓椤啴骞愭惔锝庢锤闂佺粯鍔曢幖顐ょ玻濡ゅ懎绠规繛锝庡墮婵′粙鏌涚€ｅ吀閭柡灞剧洴瀵挳濡搁妷銉ь唶闂備胶顭堥鍡涘箲閸ヮ剙绠栭柦妯侯槴閺嬫棃鏌熺粙鍨劉闁哄棛鍋ゅ缁樻媴妞嬪簼瑕嗙紓鍌氬€瑰銊╁礆閹烘垟鏋庨煫鍥ㄦ礃濞堥箖姊洪棃娑氱疄闁稿﹥娲熼悰顕€濮€閳ヨ尙绠氶梺闈涚墕閸婂憡绂嶆ィ鍐┾拺?闂傚倸鍊搁崐鐑芥嚄閸洖纾婚柕濞炬櫅绾惧潡鏌＄仦璇插姎闁藉啰鍠栭弻銊╂偄閸濆嫅銏ゆ煟?闂傚倸鍊搁崐椋庣矆娴ｉ潻鑰块梺顒€绉寸壕鍧楁煏閸繃澶勯柡鍡樼矒閺岀喖鎮滃Ο铏瑰帎闂?few-shot 婵犵數濮烽弫鎼佸磻閻愬搫鍨傞柛顐ｆ礀閽冪喖鏌曟繛鐐珦闁轰礁瀚伴弻娑樷槈濞嗘劗绋囩紓浣哄У閻楁绌辨繝鍥ч柛娑卞幗濞堟彃顪冮妶搴′簼婵炲弶绮撻獮澶愬箹娴ｅ摜鐫勯梺鍓插亝缁诲嫰鏁?Assistant:
    parts.extend(history_parts)

    if latest_user_line:
        parts.append(latest_user_line)

    # 闂傚倸鍊搁崐鐑芥嚄閸撲礁鍨濇い鏍亹閳ь剨绠撳畷濂稿Ψ閵夛附袣闂備礁鎼粙渚€宕㈡總鍛婂€块柛顭戝亖娴滄粓鏌熸潏鍓хɑ缁绢厼鐖奸弻娑㈠棘鐠恒剱褔鏌＄仦鍓ф创鐎殿噮鍓熼獮鎰償閳╁啰鏆梻鍌欐祰椤曟牠宕规导瀛樺剹闁稿本绋愮换鍡涙煟閹达絾顥夐崬顖炴⒑闂堟侗妲堕柛濠冩礋钘熸慨姗嗗厴閺€浠嬫煟閹存繃宸濋柛鎺斿缁绘稓浠﹂崒姘ｅ亾濠靛棛鏆﹂柡澶婄氨濡插牊鎱ㄥ鍡楀季婵炶偐鍠愮换娑氣偓鐢殿焾鐢爼鏌ｆ幊閸旀垵鐣烽幋锕€惟闁冲搫鍊婚崢顏呯節閻㈤潧孝缂佺粯甯￠幃楣冩焼瀹ュ棛鍘介梺瑙勫劤閻°劎绮堢€ｎ喗鐓欐い鏃€鍎抽崢瀵糕偓娈垮枟濞兼瑩锝炲┑瀣闁绘劕妯婇悗鎾⒒?闂?闂?婵犵數濮烽弫鎼佸磻閻樿绠垫い蹇撴缁躲倝鏌﹀Ο鐚寸礆婵炴垶菤閺嬪酣鏌熼悜妯虹仸婵炲牊鐓″濠氬磼濞嗘垵濡介梺璇″枛閻栫厧鐣峰┑鍡欐殕闁告洖鐏氶弲鐐烘⒑閸涘﹦顬奸柛鈺佹处缁傚秹宕烽鐔锋瀾闂婎偄娲︾粙鎴︽倿閸偁浜滈柟鐑樺灥閳ь剙缍婂畷鐢稿焵椤掑嫭鈷戦柛婵嗗婢ч亶鏌涢幘璺烘瀻闁伙絿鍏橀幃鐑芥焽閿旇棄鍏婃俊鐐€栭幐鐐垔椤撶伝娲箹娴ｅ厜鎷虹紓渚囧灡濞叉牗鏅堕弻銉︾厱闁瑰瓨绻勭粔铏光偓瑙勬礈閺佺粯鎱ㄩ埀顒勬煏閸繃顥滈柍褜鍓欓悥濂告偂椤愶箑鐐婇柕濠忕畱绾板秹姊洪悡搴㈡喐闁硅櫕鎹囧﹢渚€姊虹紒姗堣€挎繛浣冲嫮澧＄紓鍌氬€风粈渚€顢栭崨姝ゅ洭鏌嗗鍛姦濡炪倖甯掗敃锔剧矓閻㈠憡鐓曢悗锝庝簼閸ゅ洦鎱ㄦ繝鍛棄妞ゆ挸鍚嬪鍕偓锝庡墮楠炴姊绘笟鈧褔鈥﹂崼銉ョ？闂侇剙绋侀弫鍌炴煃閸濆嫬鈧崵寮ч埀顒勬偡濠婂喚妯€鐎规洘鍨块獮妯肩磼濡攱瀚奸梻浣告啞缁诲倻鈧凹鍘介崚濠冪附閸涘﹦鍘遍梺闈浤涢崟顒傚涧婵?
    # 闂?Assistant: 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁撻悩鑼唶闂佸憡绺块崕鎶芥儗閹剧粯鐓熼柕蹇嬪焺閻掗箖鏌＄€ｂ晝绐旈柡宀嬬秮楠炲洭顢楁担鐟板壍缂傚倷璁查崑鎾绘煕瀹€鈧崑鐐烘偂韫囨搩鐔嗛悹楦挎婢ф洟鏌涢弮鈧銊ф閹烘梹濯肩€规洖娲ㄩ悰銏ゆ⒑閸濆嫭婀伴柣鈺婂灦閻涱噣骞掑Δ鈧崡鎶芥煟閹扮増娑ч悽顖氭健濮婅櫣鎷犻弻銉偓妤呮煟韫囨梹鐨戦柛鐘诧工椤撳ジ宕堕埡鍐姽闂備礁婀遍崕銈夈€冮崨顓熺函闂傚倷绀佹竟濠囧磻閸℃稑绐楅幖缁版壋鍋撻幒妤€绠涙い鎾跺Х椤旀洟鏌℃径濠勫濠⒀傜矙瀹曟碍瀵肩€涙鍘梺鎼炲労閻撳牓鎮為悾宀€纾兼い鏇炴噹瀵喚鈧娲忛崝鎴︺€佸▎鎾寸叆妞ゆ牗绋撴禍娆撴⒒閸屾瑧绐旈柍褜鍓涢崑娑㈡嚐椤栨稒娅犳い鏂款潟娴滄粍銇勯幇顔夹㈤柣蹇斿絻閳规垿鏁嶉崟顐㈠箣婵犵鍓濋悺鏇⑺囬幘顔界厽闁归偊鍘界粈瀣叏?Write/Edit 闂傚倷娴囬褍顫濋敃鍌︾稏濠㈣埖鍔曠粻鏍煕椤愶絾绀€缁炬儳娼″娲敆閳ь剛绮旈幘顔藉剹婵°倕鎳忛崑锝夋煙椤撶喎绗掑┑鈥茬矙閹顫濋悡搴♀拫闂佸搫鏈惄顖炵嵁閸ヮ剙绀傞柛婵勫劚閸ゎ剟姊绘担鍛婃儓婵☆偅顨堥幑銏狀潨閳ь剙顕?
    state_notice = _build_state_followup_notice(messages, tools, client_profile)
    if state_notice:
        parts.append(state_notice)
    tool_result_notice = _build_tool_result_followup_notice(messages, tools, client_profile)
    if tool_result_notice:
        parts.append(tool_result_notice)

    parts.append("Assistant:")
    prompt = "\n\n".join(parts)
    if tools:
        if task_memory_part or dropped_history_part:
            log.info(
                "[PromptSize] total=%d tools_part=%d few_shot=%d history=%d latest=%d state_notice=%d workspace=%d task_memory=%d dropped_summary=%d tool_related=%s tool_count=%d",
                len(prompt),
                len(tools_part),
                few_shot_chars,
                used,
                len(latest_user_line),
                len(state_notice),
                len(workspace_notice),
                len(task_memory_part),
                len(dropped_history_part),
                latest_user_is_tool_related,
                len(tools),
            )
        else:
            log.info(
                "[PromptSize] total=%d tools_part=%d few_shot=%d history=%d latest=%d state_notice=%d workspace=%d tool_related=%s tool_count=%d",
                len(prompt),
                len(tools_part),
                few_shot_chars,
                used,
                len(latest_user_line),
                len(state_notice),
                len(workspace_notice),
                latest_user_is_tool_related,
                len(tools),
            )
    return prompt


_READ_VERBS = ("read", "open", "inspect", "view", "\u8bfb", "\u8bfb\u53d6", "\u67e5\u770b", "\u6253\u5f00")
_WRITE_VERBS = ("write", "create", "generate", "save", "edit", "update", "\u5199", "\u521b\u5efa", "\u751f\u6210", "\u4fdd\u5b58", "\u7f16\u8f91", "\u4fee\u6539")


def _build_state_followup_notice(messages, tools, client_profile) -> str:
    """Detect read+write intent after Read has completed and nudge toward Write/Edit."""
    if not messages or not tools or client_profile != CLAUDE_CODE_OPENAI_PROFILE:
        return ""
    # 1. Check the FIRST user message for both read + write intent.
    first_user_text = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            first_user_text = _extract_user_text_only(m.get("content", ""), client_profile=client_profile)
            if first_user_text.strip():
                break
    if not first_user_text:
        return ""
    lower = first_user_text.lower()
    wants_read = any(v in lower for v in _READ_VERBS)
    wants_write = any(v in lower for v in _WRITE_VERBS)
    if not (wants_read and wants_write):
        return ""
    # 2. Check history for at least one Read tool_use with non-trivial result, AND no Write/Edit yet.
    read_done = False
    write_done = False
    read_alias_names = {"Read", "fs_open_file", "ReadX"}
    write_alias_names = {"Write", "Edit", "NotebookEdit", "fs_put_file", "fs_patch_file", "notebook_patch", "WriteX", "EditX"}
    def _text_has_tool_alias(plain: str, aliases: set[str]) -> bool:
        for name in aliases:
            escaped = re.escape(name)
            if re.search(rf'["\']name["\']\s*:\s*["\']{escaped}["\']', plain):
                return True
            if re.search(rf'\bname\s*=\s*["\']{escaped}["\']', plain):
                return True
        return False

    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use":
                    tname = part.get("name", "")
                    if tname in read_alias_names:
                        read_done = True
                    elif tname in write_alias_names:
                        write_done = True
        # Also: scan assistant text for textual tool_use markers (Qwen bridge history renders as QNML/legacy text)
        if isinstance(m, dict) and m.get("role") == "assistant":
            plain = _extract_text(m.get("content", ""), client_profile=client_profile)
            if any(marker in plain for marker in ("<|QNML|tool_calls", "<|QNML|invoke", "<tool_calls", "<invoke", "<tool_call", "##TOOL_CALL##")):
                if _text_has_tool_alias(plain, read_alias_names):
                    read_done = True
                if _text_has_tool_alias(plain, write_alias_names):
                    write_done = True
    if not read_done or write_done:
        return ""
    return (
        "[STATE NOTICE: MUST OBEY]\n"
        "The user's CURRENT TASK explicitly requires TWO operations: reading AND writing/editing.\n"
        "You have ALREADY completed the read (the file content is in the history above).\n"
        f"Your NEXT output MUST be a {to_qwen_name('Write')}/{to_qwen_name('Edit')} tool call in the required QNML format.\n"
        "DO NOT summarize. DO NOT explain. DO NOT ask for confirmation. DO NOT output plain text.\n"
        f"If you output anything other than a <|QNML|tool_calls> block for {to_qwen_name('Write')}/{to_qwen_name('Edit')}, the user's task FAILS."
    )


def _extract_text_content(content) -> str:
    """Flatten Anthropic content array/string into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return "".join(parts)
    return ""


def _resolve_cache_hints(messages: list) -> list:
    """Replace unchanged-file tool results with cached Read content when available."""
    if not messages:
        return messages
    ctx = get_request_context()
    session_key = ctx.get("api_key", "") or ""

    # pass 1: tool_use_id -> file_path (only Read-like tools)
    toolu_to_path: dict[str, str] = {}
    READ_TOOL_NAMES = {"Read", "fs_open_file", "ReadX"}  # ReadX kept for back-compat with in-flight sessions
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use" and part.get("name") in READ_TOOL_NAMES:
                tid = part.get("id")
                fpath = (part.get("input") or {}).get("file_path") or (part.get("input") or {}).get("path")
                if tid and fpath:
                    toolu_to_path[tid] = fpath

    # pass 2: populate cache with real content AND rewrite hint-only results
    rewritten = 0
    populated = 0
    out_messages: list = []
    for msg in messages:
        if not isinstance(msg, dict):
            out_messages.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            out_messages.append(msg)
            continue
        new_content = []
        mutated = False
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                new_content.append(part)
                continue
            tid = part.get("tool_use_id", "")
            fpath = toolu_to_path.get(tid)
            inner = part.get("content", "")
            inner_text = inner if isinstance(inner, str) else _extract_text_content(inner)

            if fpath and inner_text and not file_content_cache.is_cache_hint(inner_text):
                # real content 闂?cache it
                file_content_cache.put(session_key, fpath, inner_text)
                populated += 1
                new_content.append(part)
                continue

            if fpath and inner_text and file_content_cache.is_cache_hint(inner_text):
                cached = file_content_cache.get(session_key, fpath)
                if cached:
                    new_part = dict(part)
                    # Preserve the hint as a small header so the model knows this came
                    # from the cache, followed by the real content.
                    new_part["content"] = (
                        f"[Proxy cache: previously read content of {fpath}]\n{cached}"
                    )
                    new_content.append(new_part)
                    mutated = True
                    rewritten += 1
                    continue

            new_content.append(part)
        if mutated:
            new_msg = dict(msg)
            new_msg["content"] = new_content
            out_messages.append(new_msg)
        else:
            out_messages.append(msg)

    if rewritten or populated:
        log.info(f"[CacheHint] populated={populated} rewritten={rewritten} session={'set' if session_key else 'global'}")
    return out_messages


def _apply_topic_isolation(messages: list, client_profile: str) -> list:
    """Keep only the active task span when the latest user message changes topic."""
    if not messages or len(messages) < 3:
        return messages
    # 闂傚倸鍊搁崐椋庣矆娴ｉ潻鑰块梺顒€绉撮弸渚€鏌ゆ慨鎰偓妤佺▔瀹ュ鐓涚€广儱楠搁獮鎴︽煃瑜滈崜娆撳箠韫囨洘宕叉繝闈涙－濞尖晜銇勯幒鎴濃偓鍧楁偘閹剧粯鈷掑ù锝堫潐閻忛亶鏌￠崨顔炬创鐎规洦鍨堕、娑橆煥閸涱剛鐟濋梻浣告贡閸庛倝銆冮崱娑樼９闁汇垹鎲￠悡鏇㈡煥閺冨浂鍤欐鐐寸墪闇夐柣娆忔噽閻ｇ敻鏌″畝瀣？闁逞屽墾缂嶅棙绂嶅鍫濇辈闁挎繂娲犻崑鎾斥枔閸喗鐏撻梺杞扮椤兘濡存担鑲濇棃宕ㄩ鐙呯床婵犵數鍋為崹鍓佸枈瀹ュ應鏋?user
    first_user = None
    first_user_text = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            txt = _extract_user_text_only(m.get("content", ""), client_profile=client_profile).strip()
            if txt:
                first_user = m
                first_user_text = txt
                break
    if first_user is None:
        return messages
    # 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敂钘変罕闂佸憡鍔﹂崰鏍婵犳碍鐓欓柛鎾楀懎绗￠梺缁樻尰閻╊垶寮诲☉姘勃闁告挆鍛帎婵＄偑鍊х徊浠嬪触鐎ｎ剚宕叉繝闈涱儏閻掑灚銇勯幒宥囧妽濠殿垱鎸抽幃璺衡槈閹哄棗浜鹃柛蹇撴噹椤ユ岸姊绘笟鈧鑽も偓闈涚焸瀹曘垽宕楅懖鈺婃祫闂佹寧娲栭崐褰掓偂濞戙垺鍊甸柨婵嗙凹缁ㄥ鏌￠崱顓犳偧闁逞屽墯椤旀牠宕板璺烘瀬濠电姵鍝庨埀顑跨铻栭柛娑卞幘閿涙粌鈹戦悙鏉戠仸缁炬澘绉撮埢?user 闂傚倸鍊搁崐鐑芥嚄閸洖纾婚柕濞炬櫅绾惧潡鏌＄仦璇插姎闁藉啰鍠栭弻銊╂偄閸濆嫅銏ゆ煟?濠电姴鐥夐弶搴撳亾濡や焦鍙忛柣鎴ｆ绾惧鏌ｉ幇顒佹儓缁炬儳鐏濋埞鎴﹀磼濮橆剦妫岄梺杞扮閿曨亪寮诲☉銏犖ㄩ柨婵嗘噹椤姊哄畷鍥╁笡婵☆偄鍟撮獮鍐ㄎ旈崨顔芥珳闁圭厧鐡ㄧ换鍕极閺嶎厽鈷戦柛婵嗗椤ユ粓鏌ㄩ弴銊ら偗鐎?messages 闂傚倸鍊搁崐鎼佸磹閻戣姤鍊块柨鏇氶檷娴滃綊鏌涢幇闈涙灍闁稿孩妫冮弻锝夊箻瀹曞洤鍝洪梺鍝勵儐濡啴寮婚悢琛″亾閻㈡鐒惧ù鐙呯畱閳规垿顢涘鐓庢闂侀€炲苯澧紒鐘茬Ч瀹曟洟宕￠悘缁樻そ婵℃悂鍩℃担绋挎?
    last_user = None
    last_user_text = ""
    last_user_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        m = messages[idx]
        if isinstance(m, dict) and m.get("role") == "user":
            txt = _extract_user_text_only(m.get("content", ""), client_profile=client_profile).strip()
            if txt:
                last_user = m
                last_user_text = txt
                last_user_idx = idx
                break
    if last_user is None or last_user is first_user:
        return messages
    if not detect_topic_change(first_user_text, last_user_text):
        return messages
    # 闂傚倸鍊峰ù鍥х暦閸偅鍙忛柡澶嬪殮濞差亝鏅滈柣鎰靛墮鎼村﹪姊洪崜鎻掍簴闁稿寒鍨堕崺鈧い鎺嗗亾闁稿﹤婀辩划瀣箳閺傚搫浜鹃柨婵嗙凹缁ㄨ姤銇勯敂璇蹭喊婵﹥妞介獮鏍倷閹绘帒顫戦梻浣告啞閺屻劑寮甸鍕畾闁哄啠鍋撶紒缁樼箞瀹曞爼濡歌瀵櫕绻濋悽闈涗沪闁搞劌鐖奸幃鐤樄闁诡噯绻濋、娑橆煥閸涱垽绱茬紓鍌氬€烽悞锕傛晪闂佸憡绻冨浠嬪蓟?system + 婵?last_user 闂傚倷娴囬褏鈧稈鏅犻、娆撳冀椤撶偟鐛ラ梺鍦劋椤ㄥ懐澹曟繝姘厵闁绘劦鍓氶悘閬嶆煛閳ь剟鎳為妷锝勭盎闂佸搫鍟崐鐢稿箯閿熺姵鐓曢幖杈剧磿缁犲鏌＄仦鍓ф创闁糕斁鍓濋幏鍛存惞閻熸澘袩闂佽瀛╅鏍窗濮樿泛鏋佸┑鐘冲搸閳ь兛绶氬鎾閻欌偓濞煎﹪姊洪棃娑氱濠殿噣娼ч埢鎾广亹閹烘挴鎷绘繛杈剧到濠€鍗烇耿娴犲鐓曢柡鍌濇硶閻忛亶鏌嶈閸撴岸宕欒ぐ鎺戝偍濞寸姴顑呴悿楣冩偣鏉炴媽顒熼柛姘儏椤法鎹勯悮鏉戜紣闂佺粯绻冭摫缂佺粯绻堟慨鈧柨婵嗘閵嗘劕顪冮妶鍡楃仴婵炲眰鍊濆鎶藉煛閸涱喒鎷?
    # 闂傚倸鍊搁崐鐑芥倿閿旈敮鍋撶粭娑樻噽閻瑩鏌熸潏楣冩闁稿顑夐弻鐔兼焽閿曗偓閺嬬喓鈧娲橀悡锟犲蓟閳ユ剚鍚嬮幖绮光偓宕囶啇缂傚倷璁查崑鎾垛偓鍏夊亾闁告洦鍓涢崢鍗炩攽閻愭潙鐏ョ€规洦鍓熼悰顔嘉旈崨顔惧幈闁瑰吋鐣崹鍝勭暦瀹€鍕厸鐎光偓鐎ｎ剛鐦堥悗瑙勬礃鐢繝骞冨鍫濆耿婵☆垱妞块崥瀣⒒閸屾瑨鍏岄柟铏尵缁顓兼径濠傜€梺鑺ッˇ閬嶅汲閿旂晫绡€闂傚牊渚楅崕鎰亜鎼淬垹濮嶆慨濠冩そ楠炴牠鎮欏ù瀣壕闁哄稁鍋勯崹婵堚偓鍏夊亾闁告洖鐏氶弲鈺呮椤愩垺澶勭紒瀣灴閹苯螖娴ｇ懓寮垮┑锛勫仩椤曆勭妤ｅ啯鈷戠紓浣诡焽閳洟鏌熼悷鐗堝枠鐎殿喖顭烽弫鎰緞婵犲孩缍傞梻渚€娼х换鎺撴叏閻㈠憡鍊跺ù锝囩《閺€?tool_use/tool_result 闂傚倷娴囬褍顫濋敃鍌︾稏濠㈣埖鍔曠粻鏍煕椤愶絾绀€缁炬儳娼″娲敆閳ь剛绮旈幘顔藉剹婵°倕鎳忛悡鏇犳喐鎼淬劊鈧啴宕卞☉娆忎簵闂佹寧绻傞ˇ浼存偂濞嗘挻鐓欐い鏍ф鐎氼剙鈻嶈缁辨挻鎷呴幓鎺嶅?
    system_msgs = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
    tail = messages[last_user_idx:]
    isolated = system_msgs + tail
    dropped = len(messages) - len(isolated)
    if dropped > 0:
        log.info(
            "[TopicIsolation] dropped=%s kept_tail=%s first_user=%r latest_user=%r",
            dropped, len(tail), first_user_text[:60], last_user_text[:60],
        )
    return isolated


def messages_to_prompt(req_data: dict, *, client_profile: str = OPENCLAW_OPENAI_PROFILE) -> PromptBuildResult:
    resolved_client_profile = client_profile
    raw_messages = req_data.get("messages", [])
    # 闂傚倸鍊峰ù鍥х暦閸偅鍙忛柡澶嬪殮濞差亝鏅滈柣鎰靛墮鎼村﹪姊洪崜鎻掍簴闁稿寒鍨堕崺鈧い鎺嗗亾闁稿﹤婀辩划瀣箳閺傚搫浜鹃柨婵嗛娴滄劙鏌熺粙鍨伃婵﹥妞藉畷顐﹀礋椤愶絾顔勯梻浣侯焾椤戝懎螞濠靛绠栨俊銈呮噹閽冪喖鏌曟径娑橆洭闁告瑥妫楅埞鎴︽倷閺夋垹浠搁梺鑽ゅ暀閸ャ儮鍋撻崒鐐茶摕闁靛濡囬崢鎼佹煟韫囨洖浠﹂柡瀣煼瀵劑鎳￠妶鍥╋紲闂佺鏈粙鎴犵箔瑜旈弻鐔割槹鎼粹檧鏋呭Δ鐘靛仦閹瑰洭鐛幒鎴旀斀闁搞儜鍐婵犵數濮烽弫鎼佸磻閻愬搫鍨傞柛顐ｆ礀缁犱即鏌涘☉姗嗙叕婵炲牏鏅槐鎺斺偓锝庡亽閸庛儵鏌涢悢閿嬪櫣闁宠鍨块幃鈺冣偓鍦Т椤ユ繈姊哄ú璇插箺妞ゃ劌锕濠氭晬閸曨亝鍕冮柣鐘叉处瑜板啯鎱ㄧ捄琛℃斀闁绘劘灏欓幗鐘崇箾閼碱剙鏋涚€?user 闂傚倸鍊峰ù鍥敋瑜庨〃銉х矙閸柭も偓鍧楁⒑椤掆偓缁夊澹曠紒妯圭箚妞ゆ牗绻傛禍鍦磼閳ь剚绻濋崶銊モ偓鐢告煥濠靛棝顎楅柡瀣枛閺屽秹鏌ㄧ€ｎ剙鈷岄梺鍝勬湰閻╊垶銆侀弴銏℃櫜闁糕剝鐟Σ顒佺節閻㈤潧浠滈柟鍐茬焸瀹曡绂掔€ｎ亣鎽曞┑鐐村灦閿曗晛顭囬埡鍌樹簻闁圭儤鍨甸埀顒佹倐璺柍褜鍓氱换婵堝枈婢跺瞼锛熼梺绋款儐閻╊垰鐣烽幇鏉块敜婵°倐鍋撻柡瀣╄兌閳ь剙绠嶉崕鍗灻洪敐鍛煢妞ゅ繐鐗婇悡鏇㈢叓閸ャ劎鈯曢柨娑氬枑缁绘盯骞嬮婵嬪仐闂佽鍠楅〃鍛村煝閹捐鍨傛い鏃傛櫕娴滎亪姊绘担鍛婃喐闁革絻鍎靛畷褰掓焼瀹ュ懐鏌ч柣鐘烘〃鐠€锕傚触鐎ｎ亶鐔嗛悹铏瑰皑瀹搞儵鏌ｅ┑鍤挎垹鎹㈠┑瀣潊闁挎繂妫涢妴鎰版⒑閹稿孩纾搁柛濠冪箓閻ｇ兘宕ㄦ繝鍕槇闂佹悶鍎崝搴ㄥ储閸楃偐鏀介柣鎰级椤ョ偤鎮介妤佹珚鐎规洜鏁搁埀顒婄秵閸撴稓澹曟總绋跨骇闁割偅绋戞俊璺ㄧ磼閻橀潧浠ч柍褜鍓濋～澶娒洪埡鍐濞撴埃鍋撻柕鍡曠窔瀵噣宕煎┑鍫О婵＄偑鍊曠换鎰涢銏犵柈鐎广儱顦伴埛?system + 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敂钘変罕闂佸憡鍔﹂崰鏍婵犳碍鐓欓柛鎾楀懎绗￠梺?user闂?
    # 闂傚倸鍊风粈渚€骞栭位鍥敃閿曗偓閻ょ偓绻濇繝鍌滃闁稿绻濋弻鏇熺節鎼达絽甯ラ梺鍝勬閸楀啿顫?Claude Code 闂?session 婵犵數濮烽弫鍛婃叏娴兼潙鍨傚┑鍌滎焾閺勩儵鏌″搴″箺闁稿孩顨嗛妵鍕即濡も偓娴滄儳螖閻橀潧浠﹂柛銊﹀閹便劑鍩€椤掑嫭鐓忛柛顐ｇ箖閸ｈ姤銇勯敂璇叉珝婵﹥妞藉顒勫Ψ閿旂晫褰呴梻浣告憸閸ｃ儵宕归幆鐗堫棨闁荤喐绮嶅Λ鍐嚕婵犳碍鏅搁柣妯垮皺椤︺劑姊洪崨濠冨闁稿瀚板鎼佸Χ婢跺鍘遍梺闈涚墕濞层倝鍩㈤崼銉︾厵妞ゆ棁鍋愮粔铏光偓瑙勬礀閻栧吋淇婂宀婃Ь闂佷紮绲块弫璇差潖閾忓湱鐭欓悹鎭掑妿椤旀帗绻涚€涙鐭婇柣鏍с偢閹即顢欑喊鍗炴倯婵犮垼娉涢鍛闁秵鈷戦柛鎾村絻娴滅偤鏌涢悩铏磳鐎规洏鍨介獮鍥敇閻樻鍟庨梺璇叉捣閺佹悂鈥﹂崼銉р偓閿嬩繆閻愵亜鈧牕鈻旈敃鍌氱妞ゆ巻鍋撶€殿喖娼″铏圭磼濡崵鍙嗛梺鍛婅壘椤戝鐣烽弴鐑嗗悑濠㈣泛顑囬崢閬嶆⒑閸濆嫭鍌ㄩ柛鏂跨焸閹﹢鏁撻悩宕囧弳濠电偞鍨堕悷褎鏅堕鍫熸嚉闁绘劗鍎ら悡鐔镐繆閵堝倸浜鹃梺缁樻尰濞兼瑩鈥﹂妸鈺侀唶婵犻潧鐗嗘慨?
    isolated = _apply_topic_isolation(raw_messages, resolved_client_profile)
    # Pass: 闂傚倸鍊搁崐椋庣矆娓氣偓楠炲鏁撻悩铏珨濠电姷顣藉Σ鍛村磻閸涙番鈧啯寰勯幇顑╋箓鏌熼悧鍫熺凡闂佸崬娲弻锟犲炊閳轰椒鎴峰銈嗘煥濡繂顫忓ú顏勬嵍妞ゆ挾鍋涙俊鍝勨攽椤旂》宸ユい顓炲槻閻ｇ兘鍨鹃幇浣哄弳闂佸憡娲﹂崢楣冾敊閺囥垺鈷戦悷娆忓缁€鍐╃箾閸涱喗绀嬬€规洘濞婇幖褰掑捶椤撶媴绱查梻?
    cleaned_messages, cleaned_count = clean_refusal_messages(isolated)
    if cleaned_count:
        log.info(f"[RefusalCleanup] replaced={cleaned_count} assistant messages")
    # Pass: 闂傚倸鍊搁崐椋庣矆娓氣偓楠炴牠顢曢敃鈧壕鍦磼鐎ｎ偓绱╂繛宸簼閺呮煡鏌涘☉鍙樼凹闁诲骸顭峰娲濞戞氨鐤勯梺鎼炲姀瀹曞灚绔熼弴鐔侯浄閻庯綆鍋嗛崢鐢告⒑鐠団€崇€婚柛婊冨暟缁€濠囨⒒娴ｅ憡璐￠柡鍜佸亝缁绘盯鍩€椤掑嫭鐓涢悘鐐插⒔濞叉潙鈹戦敍鍕効妞わ附褰冮湁闁绘ê纾惌鎺楁煛?
    messages = _resolve_cache_hints(cleaned_messages)
    tools = _normalize_tools(req_data.get("tools", []))
    tool_enabled = bool(tools)
    workspace_root = req_data.get("_workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root.strip():
        workspace_root = derive_workspace_root(req_data)
    system_prompt = ""
    sys_field = req_data.get("system", "")
    if isinstance(sys_field, list):
        system_prompt = " ".join(p.get("text", "") for p in sys_field if isinstance(p, dict))
    elif isinstance(sys_field, str):
        system_prompt = sys_field
    if not system_prompt:
        for msg in messages:
            if msg.get("role") == "system":
                system_prompt = _extract_text(msg.get("content", ""), client_profile=client_profile)
                break
    return PromptBuildResult(
        prompt=build_prompt_with_tools(system_prompt, messages, tools, client_profile=client_profile, workspace_root=workspace_root),
        tools=tools,
        tool_enabled=tool_enabled,
        workspace_root=workspace_root,
    )
