#!/usr/bin/env python3
import glob as globlib, json, os, re, subprocess, urllib.request, sys, time

TOOLS = {
    "glob": ("pattern", "path?"),
    "grep": ("pattern", "path?", "include?"),
    "read": ("path", "limit?"),
}

TOOL_DESC = {
    "glob": "Find files by glob pattern (e.g. **/*.py), sorted by modification time",
    "grep": "Search files for regex pattern, returns matching lines with context",
    "read": "Read file contents with line numbers",
}

SYSTEM = """You are codechat, a CLI for exploring codebases. You exist inside a project directory.

IMPORTANT: Every question is about THIS codebase. When user says "how does X work" or "show me Y", they mean in this project's code. Always use tools first to find relevant code before answering.

Rules:
- Search and read actual code - never guess or assume
- Show snippets with file:line references
- Explain like a senior dev in a code review
- Be direct, no fluff
- "show me the code for X" = grep/glob then read the relevant files
- Follow-up questions ("show me that", "how does the above work") refer to code you just discussed

Tools: glob (find files), grep (search content), read (view file)."""

MIN_REQUEST_INTERVAL = 1.0
last_request_time = 0

class C:
    reset = "\033[0m"
    bold = "\033[1m"
    dim = "\033[2m"
    italic = "\033[3m"
    blue = "\033[38;5;75m"
    green = "\033[38;5;114m"
    yellow = "\033[38;5;221m"
    purple = "\033[38;5;183m"
    cyan = "\033[38;5;117m"
    orange = "\033[38;5;215m"
    gray = "\033[38;5;250m"
    white = "\033[38;5;255m"
    red = "\033[38;5;210m"
    bg = "\033[48;5;237m"

def log(msg, style="info"):
    icons = {
        "info": f"{C.blue}●{C.reset}",
        "ok": f"{C.green}✓{C.reset}",
        "warn": f"{C.yellow}⚠{C.reset}",
        "wait": f"{C.purple}◌{C.reset}",
        "tool": f"{C.orange}▸{C.reset}"
    }
    print(f"  {icons.get(style, icons['info'])} {C.gray}{msg}{C.reset}")

def render(text):
    lines = text.split('\n')
    out = []
    in_code = False
    code_lang = ""
    code_buf = []

    for line in lines:
        if line.startswith('```'):
            if not in_code:
                in_code = True
                code_lang = line[3:].strip()
                code_buf = []
            else:
                in_code = False
                if code_buf:
                    out.append(f"\n  {C.gray}{'─'*3} {code_lang or 'code'} {'─'*40}{C.reset}")
                    for i, cl in enumerate(code_buf):
                        num = f"{C.gray}{i+1:3}│{C.reset}"
                        out.append(f"  {num} {C.bg} {C.white}{cl}{' '*(80-len(cl))} {C.reset}")
                    out.append(f"  {C.gray}{'─'*50}{C.reset}\n")
            continue

        if in_code:
            code_buf.append(line)
        else:
            s = line
            s = re.sub(r'\*\*(.+?)\*\*', f'{C.bold}\\1{C.reset}', s)
            s = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', f'{C.italic}\\1{C.reset}', s)
            s = re.sub(r'`([^`]+)`', f'{C.bg}{C.cyan} \\1 {C.reset}', s)
            s = re.sub(r'^(#{1,3})\s+(.+)$', f'{C.bold}{C.purple}\\2{C.reset}', s)
            s = re.sub(r'^(\d+\.)\s+', f'{C.yellow}\\1{C.reset} ', s)
            s = re.sub(r'^(\s*)[-*]\s+', f'\\1{C.yellow}•{C.reset} ', s)
            out.append(s)

    if in_code and code_buf:
        for cl in code_buf:
            out.append(f"  {C.bg} {C.white}{cl} {C.reset}")

    print('\n'.join(out))

def run_tool(name, args):
    try:
        if name == "glob":
            files = sorted(globlib.glob(args.get("path", ".") + "/" + args["pattern"], recursive=True), key=lambda f: -os.path.getmtime(f) if os.path.exists(f) else 0)
            return "\n".join(files[:100]) or "No files found"
        elif name == "grep":
            cmd = ["grep", "-rn", "--include=" + args.get("include", "*"), "-E", args["pattern"], args.get("path", ".")]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            lines = result.stdout.strip().split("\n")[:50]
            return "\n".join(lines) or "No matches found"
        elif name == "read":
            with open(args["path"]) as f:
                lines = f.readlines()[:int(args.get("limit", 500))]
            return "".join(f"{i+1:4}| {line}" for i, line in enumerate(lines))
    except Exception as e:
        return f"Error: {e}"

def make_schema(name, params):
    props, req = {}, []
    for p in params:
        key = p.rstrip("?")
        props[key] = {"type": "string"}
        if not p.endswith("?"):
            req.append(key)
    return {"type": "object", "properties": props, "required": req}

def fetch(req, retries=3):
    global last_request_time
    elapsed = time.time() - last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        wait = MIN_REQUEST_INTERVAL - elapsed
        log(f"throttle {wait:.1f}s", "wait")
        time.sleep(wait)

    for i in range(retries):
        try:
            log(f"request → {req.host}", "info")
            last_request_time = time.time()
            resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
            log("response received", "ok")
            return resp
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < retries - 1:
                wait = 2 ** (i + 2)
                log(f"rate limited, retry in {wait}s", "warn")
                time.sleep(wait)
            else:
                raise

def call_anthropic(messages):
    tools = [{"name": n, "description": TOOL_DESC[n], "input_schema": make_schema(n, p)} for n, p in TOOLS.items()]
    body = {"model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"), "max_tokens": 8192, "system": SYSTEM, "messages": messages, "tools": tools}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", json.dumps(body).encode(), {"Content-Type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
    resp = fetch(req)
    return resp["content"], resp.get("stop_reason")

def call_gemini(messages):
    tools = [{"functionDeclarations": [{"name": n, "description": TOOL_DESC[n], "parameters": make_schema(n, p)} for n, p in TOOLS.items()]}]
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        parts = []
        for c in (m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]):
            if c.get("type") == "text":
                parts.append({"text": c["text"]})
            elif c.get("type") == "tool_use":
                parts.append({"functionCall": {"name": c["name"], "args": c["input"]}})
            elif c.get("type") == "tool_result":
                parts.append({"functionResponse": {"name": c.get("tool_use_id", "tool"), "response": {"result": c["content"]}}})
        if parts:
            contents.append({"role": role, "parts": parts})
    body = {"contents": contents, "tools": tools, "systemInstruction": {"parts": [{"text": SYSTEM}]}}
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    resp = fetch(req)
    content, stop = [], "end_turn"
    for part in resp.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "text" in part:
            content.append({"type": "text", "text": part["text"]})
        elif "functionCall" in part:
            content.append({"type": "tool_use", "id": part["functionCall"]["name"], "name": part["functionCall"]["name"], "input": dict(part["functionCall"].get("args", {}))})
            stop = "tool_use"
    return content, stop

def call_api(messages):
    provider = os.getenv("CODECHAT_PROVIDER", "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "gemini")
    if provider == "anthropic":
        return call_anthropic(messages)
    else:
        return call_gemini(messages)

def chat(messages):
    while True:
        content, stop = call_api(messages)
        messages.append({"role": "assistant", "content": content})
        for c in content:
            if c.get("type") == "text":
                print()
                render(c["text"])
            elif c.get("type") == "tool_use":
                args_str = " ".join(f"{C.cyan}{k}{C.reset}={C.white}{v}{C.reset}" for k, v in c["input"].items())
                log(f"{C.bold}{c['name']}{C.reset} {args_str}", "tool")
                result = run_tool(c["name"], c["input"])
                lines = result.split('\n')
                preview = '\n'.join(f"  {C.gray}│{C.reset} {C.dim}{l}{C.reset}" for l in lines[:6])
                if len(lines) > 6:
                    preview += f"\n  {C.gray}│ ... +{len(lines)-6} more lines{C.reset}"
                print(preview)
                messages[-1]["content"].append({"type": "tool_result", "tool_use_id": c["id"], "content": result})
        if stop != "tool_use":
            break
    return messages

def main():
    print(f"\n{C.bold}{C.purple}codechat{C.reset} {C.gray}— talk to your codebase{C.reset}")
    print(f"{C.gray}Commands: /q quit, /c clear{C.reset}")
    provider = os.getenv("CODECHAT_PROVIDER", "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "gemini")
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514") if provider == "anthropic" else os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    print(f"{C.gray}Using {C.green}{provider}{C.gray} ({model}){C.reset}\n")

    messages = []
    while True:
        try:
            user = input(f"{C.bold}{C.blue}>{C.reset} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user == "/q":
            break
        if user == "/c":
            messages = []
            print(f"{C.green}Cleared.{C.reset}")
            continue
        messages.append({"role": "user", "content": user})
        try:
            messages = chat(messages)
        except Exception as e:
            print(f"{C.red}Error: {e}{C.reset}")
            messages.pop()
        print()

if __name__ == "__main__":
    main()
