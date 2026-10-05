"""
游戏内着色器编译与缓存系统 - 核心模块

功能：
- compile_shader: 着色器编译（预处理：宏/包含；优化：注释/死代码/空白）
- shader_cache:   编译结果缓存（内存 + 磁盘持久化 + 源码变化失效）
- shader_variants: 着色器变体（排列组合生成 + 裁剪）
- shader_hot_reload: 热重载（文件监视 + 渲染状态保持）
- shader_debug:   调试（打印 + 断点）
"""
import hashlib
import itertools
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class Config:
    cache_dir: Optional[str] = None
    optimize: bool = True
    debug: bool = False


class ShaderCompileError(Exception):
    pass


_CACHE_FILE = "shader_cache.json"


class ShaderCompiler:
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.cache_dir = self.config.get("cache_dir")
        self.optimize_enabled = self.config.get("optimize", True)
        self.debug_enabled = self.config.get("debug", False)

        # 编译缓存：key -> 编译结果
        self._cache: Dict[str, Dict] = {}
        # 渲染状态（热重载后保持）
        self._state: Dict = {}
        self._history: List = []
        # 监视的着色器文件：path -> mtime
        self._watched: Dict[str, float] = {}
        # 已加载的程序：name -> 编译结果
        self._programs: Dict[str, Dict] = {}
        # 调试
        self._breakpoints: Dict[int, Optional[Callable[[str], bool]]] = {}
        self._debug_log: List[str] = []

        if self.cache_dir:
            self.load_cache()

    # ------------------------------------------------------------------
    # 框架接口
    # ------------------------------------------------------------------
    def update(self, dt: float):
        for path in self.poll_changes():
            self._history.append(("reloaded", path, time.time()))
            self.hot_reload(path)

    def reset(self):
        self._state = {}
        self._history = []
        self._watched = {}
        self._programs = {}
        self._breakpoints = {}
        self._debug_log = []

    # ------------------------------------------------------------------
    # 1. 着色器编译：预处理（宏 + 包含）与优化
    # ------------------------------------------------------------------
    def preprocess(self, source: str,
                   defines: Optional[Dict[str, str]] = None,
                   include_resolver: Optional[Callable[[str], str]] = None,
                   _depth: int = 0) -> str:
        """处理 #define/#undef/#if/#ifdef/#ifndef/#elif/#else/#endif/#include 与宏替换。"""
        if _depth > 32:
            raise ShaderCompileError("include 嵌套过深")
        macros: Dict[str, str] = {str(k): str(v) for k, v in (defines or {}).items()}
        out: List[str] = []
        # 条件栈：(父级是否生效, 当前分支是否生效, 是否已有分支生效)
        cond_stack: List[Tuple[bool, bool, bool]] = []

        def active() -> bool:
            return all(frame[1] for frame in cond_stack)

        def eval_expr(expr: str) -> bool:
            expr = re.sub(r"defined\((\w+)\)", r"\1", expr)
            expr = re.sub(r"defined\s+(\w+)", r"\1", expr)
            def repl(m):
                name = m.group(0)
                if name in ("and", "or", "not"):
                    return name
                val = macros.get(name, "0")
                return val if re.fullmatch(r"-?\d+", val) else "0"
            expr = re.sub(r"\b[A-Za-z_]\w*\b", repl, expr)
            expr = expr.replace("&&", " and ").replace("||", " or ")
            expr = re.sub(r"!(?!=)", " not ", expr)
            try:
                return bool(eval(expr, {"__builtins__": {}}, {}))
            except Exception:
                return False

        for raw in source.splitlines():
            line = raw.strip()
            m = re.match(r"#\s*(\w+)\s*(.*)", line)
            if m:
                directive, arg = m.group(1), m.group(2).strip()
                if directive == "define":
                    if active():
                        parts = arg.split(None, 1)
                        macros[parts[0]] = parts[1] if len(parts) > 1 else "1"
                    continue
                if directive == "undef":
                    if active():
                        macros.pop(arg, None)
                    continue
                if directive in ("ifdef", "ifndef", "if"):
                    parent = active()
                    if directive == "if":
                        cond = eval_expr(arg)
                    elif directive == "ifdef":
                        cond = arg in macros
                    else:
                        cond = arg not in macros
                    cond_stack.append((parent, parent and cond, cond))
                    continue
                if directive == "elif":
                    if not cond_stack:
                        raise ShaderCompileError("#elif 没有匹配的 #if")
                    parent, _, taken = cond_stack.pop()
                    cond = (not taken) and eval_expr(arg)
                    cond_stack.append((parent, parent and cond, taken or cond))
                    continue
                if directive == "else":
                    if not cond_stack:
                        raise ShaderCompileError("#else 没有匹配的 #if")
                    parent, _, taken = cond_stack.pop()
                    cond_stack.append((parent, parent and not taken, True))
                    continue
                if directive == "endif":
                    if not cond_stack:
                        raise ShaderCompileError("#endif 没有匹配的 #if")
                    cond_stack.pop()
                    continue
                if directive == "include":
                    if not active():
                        continue
                    if include_resolver is None:
                        raise ShaderCompileError("没有 include_resolver，无法处理 #include")
                    name = arg.strip().strip('"').strip("<>")
                    included = include_resolver(name)
                    if included is None:
                        raise ShaderCompileError("找不到包含文件: %s" % name)
                    out.append(self.preprocess(included, macros, include_resolver, _depth + 1))
                    continue
                # 其他指令（#version 等）原样保留
                if active():
                    out.append(line)
                continue
            if active():
                out.append(self._expand_macros(raw, macros))

        if cond_stack:
            raise ShaderCompileError("#if 没有匹配的 #endif")
        return "\n".join(out)

    @staticmethod
    def _expand_macros(line: str, macros: Dict[str, str]) -> str:
        for name, value in macros.items():
            line = re.sub(r"\b%s\b" % re.escape(name), value, line)
        return line

    def optimize(self, source: str) -> str:
        """去除注释、空行与多余空白，并折叠常量算术表达式。"""
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        lines = []
        for raw in source.splitlines():
            line = re.sub(r"//.*", "", raw)
            line = re.sub(r"\s+", " ", line).strip()
            if line:
                lines.append(line)
        source = "\n".join(lines)

        def fold(m):
            try:
                val = eval(m.group(0), {"__builtins__": {}}, {})
                return repr(float(val)) if isinstance(val, float) else str(val)
            except Exception:
                return m.group(0)
        return re.sub(r"\b\d+\.\d+(?:\s*[-+*/]\s*\d+\.\d+)+\b", fold, source)

    @staticmethod
    def _cache_key(source: str, defines: Optional[Dict] = None) -> str:
        h = hashlib.sha256()
        h.update(source.encode("utf-8"))
        h.update(json.dumps(defines or {}, sort_keys=True).encode("utf-8"))
        return h.hexdigest()

    def compile_shader(self, source: str,
                       defines: Optional[Dict[str, str]] = None,
                       include_resolver: Optional[Callable[[str], str]] = None,
                       optimize: Optional[bool] = None,
                       use_cache: bool = True) -> Dict:
        """编译着色器：预处理 -> 优化 -> 缓存。返回编译结果字典。"""
        key = self._cache_key(source, defines)
        if use_cache and key in self._cache:
            cached = dict(self._cache[key])
            cached["from_cache"] = True
            return cached

        preprocessed = self.preprocess(source, defines, include_resolver)
        do_opt = self.optimize_enabled if optimize is None else optimize
        code = self.optimize(preprocessed) if do_opt else preprocessed
        result = {
            "code": code,
            "preprocessed": preprocessed,
            "hash": key,
            "defines": dict(defines or {}),
            "optimized": bool(do_opt),
            "compiled_at": time.time(),
            "from_cache": False,
        }
        if use_cache:
            self._cache[key] = {k: v for k, v in result.items() if k != "from_cache"}
            self.save_cache()
        return result

    # ------------------------------------------------------------------
    # 2. 着色器缓存：持久化与失效
    # ------------------------------------------------------------------
    def cache_get(self, source: str, defines: Optional[Dict] = None) -> Optional[Dict]:
        entry = self._cache.get(self._cache_key(source, defines))
        return dict(entry, from_cache=True) if entry else None

    def cache_put(self, source: str, defines: Optional[Dict], compiled: Dict) -> None:
        self._cache[self._cache_key(source, defines)] = dict(compiled)
        self.save_cache()

    def invalidate(self, source: str, defines: Optional[Dict] = None) -> bool:
        """源码变化后使对应缓存失效。返回是否有缓存被移除。"""
        key = self._cache_key(source, defines)
        if key in self._cache:
            del self._cache[key]
            self.save_cache()
            return True
        return False

    def invalidate_all(self) -> None:
        self._cache.clear()
        self.save_cache()

    def cache_size(self) -> int:
        return len(self._cache)

    def save_cache(self) -> None:
        if not self.cache_dir:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        path = os.path.join(self.cache_dir, _CACHE_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._cache, f)
        os.replace(tmp, path)

    def load_cache(self) -> int:
        path = os.path.join(self.cache_dir, _CACHE_FILE)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._cache.update(json.load(f))
        return len(self._cache)

    # ------------------------------------------------------------------
    # 3. 着色器变体：排列组合与裁剪
    # ------------------------------------------------------------------
    def generate_variants(self, macro_space: Dict[str, List]) -> List[Dict]:
        """根据宏取值空间生成全部排列组合的变体定义列表。"""
        if not macro_space:
            return [{}]
        keys = list(macro_space.keys())
        return [dict(zip(keys, combo))
                for combo in itertools.product(*(macro_space[k] for k in keys))]

    def strip_variants(self, variants: List[Dict],
                       keep: Optional[Callable[[Dict], bool]] = None,
                       forbid: Optional[List[Dict]] = None) -> List[Dict]:
        """裁剪变体：keep 谓词保留需要的变体；forbid 列出禁止的宏组合。"""
        result = []
        for variant in variants:
            if keep is not None and not keep(variant):
                continue
            if forbid and any(all(variant.get(k) == v for k, v in rule.items())
                              for rule in forbid):
                continue
            result.append(variant)
        return result

    def compile_variants(self, source: str,
                         macro_space: Dict[str, List],
                         keep: Optional[Callable[[Dict], bool]] = None,
                         forbid: Optional[List[Dict]] = None,
                         include_resolver: Optional[Callable[[str], str]] = None
                         ) -> Dict[str, Dict]:
        """生成并编译裁剪后的全部变体，返回 {变体键: 编译结果}。"""
        variants = self.strip_variants(self.generate_variants(macro_space), keep, forbid)
        compiled = {}
        for variant in variants:
            key = ",".join("%s=%s" % (k, variant[k]) for k in sorted(variant)) or "default"
            compiled[key] = self.compile_shader(
                source, defines={k: str(v) for k, v in variant.items()},
                include_resolver=include_resolver)
        return compiled

    # ------------------------------------------------------------------
    # 4. 着色器热重载：文件监视与状态保持
    # ------------------------------------------------------------------
    def watch_file(self, path: str) -> None:
        self._watched[path] = os.path.getmtime(path)

    def unwatch_file(self, path: str) -> None:
        self._watched.pop(path, None)

    def poll_changes(self) -> List[str]:
        """检查被监视文件是否变化，返回发生变化的文件路径列表。"""
        changed = []
        for path, old_mtime in list(self._watched.items()):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime != old_mtime:
                self._watched[path] = mtime
                changed.append(path)
        return changed

    def hot_reload(self, path: str,
                   defines: Optional[Dict[str, str]] = None,
                   include_resolver: Optional[Callable[[str], str]] = None) -> Dict:
        """重新编译文件并更新程序，渲染状态 self._state 保持不变。"""
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        result = self.compile_shader(source, defines=defines,
                                     include_resolver=include_resolver,
                                     use_cache=False)
        self._programs[path] = result
        self._watched[path] = os.path.getmtime(path)
        self._history.append(("reload", path, result["hash"]))
        return result

    def get_program(self, path: str) -> Optional[Dict]:
        return self._programs.get(path)

    def set_render_state(self, **state) -> None:
        self._state.update(state)

    def get_render_state(self) -> Dict:
        return dict(self._state)

    # ------------------------------------------------------------------
    # 5. 着色器调试：打印与断点
    # ------------------------------------------------------------------
    def debug_print(self, fmt: str, *args) -> str:
        """在着色器调试通道中打印信息，记录到调试日志。"""
        try:
            message = fmt % args if args else fmt
        except (TypeError, ValueError):
            message = " ".join([fmt] + [str(a) for a in args])
        self._debug_log.append(message)
        return message

    @property
    def debug_log(self) -> List[str]:
        return list(self._debug_log)

    def clear_debug_log(self) -> None:
        self._debug_log = []

    def set_breakpoint(self, line: int,
                       condition: Optional[Callable[[str], bool]] = None) -> None:
        """在着色器源码的指定行设置断点，可附加条件（接收该行文本）。"""
        self._breakpoints[line] = condition

    def clear_breakpoint(self, line: int) -> None:
        self._breakpoints.pop(line, None)

    def clear_breakpoints(self) -> None:
        self._breakpoints.clear()

    def debug_run(self, source: str) -> Dict:
        """逐行模拟执行着色器源码，命中断点时暂停并记录。

        返回 {"hits": [(行号, 行文本), ...], "prints": [...]}；
        行内形如 debug_print("msg", ...) 的调用会被捕获到 prints。
        """
        hits = []
        prints = []
        for lineno, line in enumerate(source.splitlines(), start=1):
            for m in re.finditer(r'debug_print\("([^"]*)"\s*(?:,\s*([^)]*))?\)', line):
                fmt, rest = m.group(1), m.group(2)
                args = tuple(a.strip() for a in rest.split(",")) if rest else ()
                prints.append(self.debug_print(fmt, *args))
            if lineno in self._breakpoints:
                condition = self._breakpoints[lineno]
                if condition is None or condition(line):
                    hits.append((lineno, line.strip()))
        return {"hits": hits, "prints": prints}
