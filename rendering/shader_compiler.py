"""
游戏内着色器编译与缓存系统 - 核心模块

功能：
- 着色器编译：预处理（宏/包含/条件编译）+ 优化
- 着色器缓存：内存缓存 + 磁盘持久化 + 源码失效
- 着色器变体：排列组合生成 + 变体裁剪
- 着色器热重载：文件监视 + 渲染状态保持
- 着色器调试：调试信息 + 打印注入 + 断点
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
import hashlib
import itertools
import json
import os
import re
import time


@dataclass
class Config:
    pass


@dataclass
class CompiledShader:
    """一次编译的产物。"""
    name: str
    source: str                 # 预处理 + 优化后的源码
    bytecode: str               # 模拟字节码（源码哈希）
    defines: Dict[str, str]
    source_hash: str
    compile_time: float
    from_cache: bool = False
    debug_info: Optional[Dict] = None


class ShaderCompiler:
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._state = {}
        self._history = []
        # 缓存
        self.cache_dir = self.config.get("cache_dir")
        self._cache: Dict[str, Dict] = {}
        if self.cache_dir:
            self.load_cache()
        # 热重载
        self._file_mtimes: Dict[str, float] = {}
        self._file_sources: Dict[str, str] = {}
        self._watchers: Dict[str, List[Tuple[Optional[Callable], Dict, Dict]]] = {}
        # 变体
        self._variants: Dict[str, List[CompiledShader]] = {}
        # 渲染状态（uniforms）
        self._uniforms: Dict[str, object] = {}
        # 调试
        self.debug_enabled = False
        self._breakpoints: Dict[str, set] = {}
        self._debug_log: List[str] = []

    # ------------------------------------------------------------------
    # 基础接口
    # ------------------------------------------------------------------
    def update(self, dt: float):
        self.check_updates()

    def reset(self):
        self._state = {}
        self._history = []

    # ------------------------------------------------------------------
    # 着色器编译：预处理 + 优化
    # ------------------------------------------------------------------
    def preprocess(self, source: str, defines: Optional[Dict[str, str]] = None,
                   includes: Optional[Dict[str, str]] = None) -> str:
        """处理 #define / #ifdef / #ifndef / #else / #endif / #include 与宏替换。"""
        defines = dict(defines or {})
        includes = includes or {}
        out_lines: List[str] = []
        # 条件栈元素: (父级是否激活, 本分支条件是否为真, 是否已进入else)
        stack: List[Tuple[bool, bool, bool]] = []
        active = True

        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#include"):
                if active:
                    match = re.search(r'["<]([^">]+)[">]', stripped)
                    if not match:
                        raise ValueError("非法的 #include 指令: %s" % stripped)
                    name = match.group(1)
                    if name not in includes:
                        raise KeyError("找不到包含文件: %s" % name)
                    out_lines.append(self.preprocess(includes[name], defines, includes))
                continue
            if stripped.startswith("#define"):
                if active:
                    parts = stripped.split(None, 2)
                    if len(parts) < 2:
                        raise ValueError("非法的 #define 指令: %s" % stripped)
                    defines[parts[1]] = parts[2] if len(parts) > 2 else "1"
                continue
            if stripped.startswith("#ifdef"):
                cond = stripped.split(None, 1)[1].strip() in defines
                stack.append((active, cond, False))
                active = active and cond
                continue
            if stripped.startswith("#ifndef"):
                cond = stripped.split(None, 1)[1].strip() not in defines
                stack.append((active, cond, False))
                active = active and cond
                continue
            if stripped.startswith("#if "):
                expr = stripped.split(None, 1)[1].strip()
                cond = bool(defines.get(expr)) and defines.get(expr) != "0"
                stack.append((active, cond, False))
                active = active and cond
                continue
            if stripped.startswith("#else"):
                if not stack:
                    raise ValueError("#else 没有匹配的 #ifdef")
                parent, cond, _ = stack[-1]
                stack[-1] = (parent, cond, True)
                active = parent and not cond
                continue
            if stripped.startswith("#endif"):
                if not stack:
                    raise ValueError("#endif 没有匹配的 #ifdef")
                parent, _, _ = stack.pop()
                active = parent
                continue
            if active:
                out_lines.append(line)

        if stack:
            raise ValueError("#ifdef 缺少匹配的 #endif")

        text = "\n".join(out_lines)
        # 宏替换（整词替换，长名字优先避免子串误替换）
        for name in sorted(defines, key=len, reverse=True):
            text = re.sub(r"\b%s\b" % re.escape(name), defines[name], text)
        return text

    def optimize(self, source: str) -> str:
        """去除注释/空行，并做简单的常量化简。"""
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        source = re.sub(r"//[^\n]*", "", source)
        lines = []
        for line in source.splitlines():
            line = line.strip()
            if not line:
                continue
            # x * 1.0 -> x ; 1.0 * x -> x
            line = re.sub(r"\b([A-Za-z_]\w*)\s*\*\s*1(?:\.0+)?\b", r"\1", line)
            line = re.sub(r"\b1(?:\.0+)?\s*\*\s*([A-Za-z_]\w*)\b", r"\1", line)
            # x + 0.0 -> x ; 0.0 + x -> x
            line = re.sub(r"\b([A-Za-z_]\w*)\s*\+\s*0(?:\.0+)?\b", r"\1", line)
            line = re.sub(r"\b0(?:\.0+)?\s*\+\s*([A-Za-z_]\w*)\b", r"\1", line)
            lines.append(line)
        return "\n".join(lines)

    def compile_shader(self, source: str, defines: Optional[Dict[str, str]] = None,
                       includes: Optional[Dict[str, str]] = None,
                       optimize: bool = True, name: str = "shader",
                       debug: bool = False, use_cache: bool = True) -> CompiledShader:
        """编译着色器：预处理 -> 优化 -> 生成字节码，结果写入缓存。"""
        defines = dict(defines or {})
        start = time.perf_counter()
        processed = self.preprocess(source, defines, includes)
        if optimize:
            processed = self.optimize(processed)
        key = self._hash(processed + ("#debug" if debug else ""))

        if use_cache and key in self._cache:
            entry = self._cache[key]
            return CompiledShader(
                name=entry["name"], source=entry["source"],
                bytecode=entry["bytecode"], defines=entry["defines"],
                source_hash=key, compile_time=entry["compile_time"],
                from_cache=True, debug_info=entry.get("debug_info"),
            )

        bytecode = hashlib.sha256(processed.encode("utf-8")).hexdigest()
        compile_time = time.perf_counter() - start
        debug_info = None
        if debug or self.debug_enabled:
            debug_info = {
                "line_count": len(processed.splitlines()),
                "lines": processed.splitlines(),
                "defines": dict(defines),
            }
        shader = CompiledShader(
            name=name, source=processed, bytecode=bytecode,
            defines=defines, source_hash=key, compile_time=compile_time,
            from_cache=False, debug_info=debug_info,
        )
        self._cache[key] = {
            "name": name, "source": processed, "bytecode": bytecode,
            "defines": defines, "compile_time": compile_time,
            "debug_info": debug_info,
        }
        self._history.append({"op": "compile", "name": name, "hash": key})
        return shader

    # ------------------------------------------------------------------
    # 着色器缓存：持久化 + 失效
    # ------------------------------------------------------------------
    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def cache_size(self) -> int:
        return len(self._cache)

    def is_cached(self, source: str, defines: Optional[Dict[str, str]] = None,
                  includes: Optional[Dict[str, str]] = None) -> bool:
        processed = self.optimize(self.preprocess(source, dict(defines or {}), includes))
        return self._hash(processed) in self._cache

    def invalidate(self, source: str, defines: Optional[Dict[str, str]] = None,
                   includes: Optional[Dict[str, str]] = None) -> bool:
        """按源码使缓存失效；源码变更后旧缓存条目被移除。"""
        processed = self.optimize(self.preprocess(source, dict(defines or {}), includes))
        key = self._hash(processed)
        if key in self._cache:
            del self._cache[key]
            self._history.append({"op": "invalidate", "hash": key})
            return True
        return False

    def invalidate_all(self):
        self._cache.clear()

    def save_cache(self):
        """把缓存持久化到磁盘。"""
        if not self.cache_dir:
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        path = os.path.join(self.cache_dir, "shader_cache.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._cache, f)

    def load_cache(self):
        """从磁盘恢复缓存（重启后不丢失）。"""
        if not self.cache_dir:
            return
        path = os.path.join(self.cache_dir, "shader_cache.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._cache = json.load(f)

    # ------------------------------------------------------------------
    # 着色器变体：排列组合 + 裁剪
    # ------------------------------------------------------------------
    def generate_variants(self, keywords: Dict[str, List[str]]) -> List[Dict[str, str]]:
        """对关键字取值做笛卡尔积，生成全部变体排列。"""
        names = list(keywords.keys())
        variants = []
        for values in itertools.product(*(keywords[n] for n in names)):
            variants.append(dict(zip(names, values)))
        return variants

    def strip_variants(self, variants: List[Dict[str, str]],
                       used: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """裁剪掉实际未使用的变体，只保留 used 中列出的组合。"""
        used_keys = {tuple(sorted(u.items())) for u in used}
        return [v for v in variants if tuple(sorted(v.items())) in used_keys]

    def compile_variants(self, name: str, source: str,
                         keywords: Dict[str, List[str]],
                         used: Optional[List[Dict[str, str]]] = None) -> List[CompiledShader]:
        """编译全部（或裁剪后的）变体并登记。"""
        variants = self.generate_variants(keywords)
        if used is not None:
            variants = self.strip_variants(variants, used)
        compiled = []
        for combo in variants:
            defines = {"%s_%s" % (k, v): "1" for k, v in combo.items()}
            variant_name = "%s#%s" % (name, ",".join("%s=%s" % kv for kv in sorted(combo.items())))
            compiled.append(self.compile_shader(source, defines=defines, name=variant_name))
        self._variants[name] = compiled
        return compiled

    def get_variants(self, name: str) -> List[CompiledShader]:
        return self._variants.get(name, [])

    # ------------------------------------------------------------------
    # 着色器热重载：文件监视 + 状态保持
    # ------------------------------------------------------------------
    def watch_file(self, path: str, callback: Optional[Callable] = None,
                   defines: Optional[Dict[str, str]] = None,
                   includes: Optional[Dict[str, str]] = None):
        """注册文件监视，记录初始内容与修改时间。"""
        with open(path, "r", encoding="utf-8") as f:
            self._file_sources[path] = f.read()
        self._file_mtimes[path] = os.path.getmtime(path)
        self._watchers.setdefault(path, []).append((callback, dict(defines or {}), includes or {}))

    def check_updates(self) -> List[str]:
        """轮询被监视文件，重载发生变化的文件，返回重载路径列表。"""
        reloaded = []
        for path, old_mtime in list(self._file_mtimes.items()):
            if not os.path.exists(path):
                continue
            if os.path.getmtime(path) != old_mtime:
                self.reload_file(path)
                reloaded.append(path)
        return reloaded

    def reload_file(self, path: str) -> List[CompiledShader]:
        """热重载单个文件：失效旧缓存、重新编译、保持渲染状态。"""
        with open(path, "r", encoding="utf-8") as f:
            new_source = f.read()
        old_source = self._file_sources.get(path)

        # 源码变更 -> 旧缓存条目失效
        if old_source is not None and old_source != new_source:
            for _, defines, includes in self._watchers.get(path, []):
                self.invalidate(old_source, defines, includes)

        self._file_sources[path] = new_source
        self._file_mtimes[path] = os.path.getmtime(path)

        # 状态保持：重载前快照渲染状态，重载后恢复
        saved_uniforms = dict(self._uniforms)
        results = []
        for callback, defines, includes in self._watchers.get(path, []):
            shader = self.compile_shader(
                new_source, defines=defines, includes=includes,
                name=os.path.basename(path),
            )
            results.append(shader)
            if callback:
                callback(shader)
        self._uniforms = saved_uniforms
        self._state["uniforms"] = dict(saved_uniforms)
        self._history.append({"op": "reload", "path": path})
        return results

    # 渲染状态（uniforms）
    def set_uniform(self, name: str, value):
        self._uniforms[name] = value

    def get_uniform(self, name: str, default=None):
        return self._uniforms.get(name, default)

    def save_state(self):
        self._state["saved_uniforms"] = dict(self._uniforms)

    def restore_state(self):
        self._uniforms.update(self._state.get("saved_uniforms", {}))

    # ------------------------------------------------------------------
    # 着色器调试：打印 + 断点
    # ------------------------------------------------------------------
    def enable_debug(self, enabled: bool = True):
        self.debug_enabled = enabled

    def inject_debug_print(self, source: str, variable: str,
                           label: Optional[str] = None) -> str:
        """在变量赋值语句后注入 debug_print 调试输出。"""
        label = label or variable
        out_lines = []
        pattern = re.compile(r"\b%s\s*=" % re.escape(variable))
        for line in source.splitlines():
            out_lines.append(line)
            if pattern.search(line) and "debug_print" not in line:
                out_lines.append('debug_print("%s", %s);' % (label, variable))
        return "\n".join(out_lines)

    def debug_print(self, label: str, value) -> str:
        msg = "[DEBUG] %s = %s" % (label, value)
        self._debug_log.append(msg)
        return msg

    def get_debug_log(self) -> List[str]:
        return list(self._debug_log)

    def set_breakpoint(self, shader_name: str, line: int):
        self._breakpoints.setdefault(shader_name, set()).add(line)

    def clear_breakpoint(self, shader_name: str, line: int):
        self._breakpoints.get(shader_name, set()).discard(line)

    def debug_run(self, shader: CompiledShader,
                  inputs: Optional[Dict[str, object]] = None) -> List[Tuple]:
        """模拟逐行执行：触发断点并收集 debug_print 输出。"""
        inputs = inputs or {}
        trace: List[Tuple] = []
        breakpoints = self._breakpoints.get(shader.name, set())
        for lineno, line in enumerate(shader.source.splitlines(), start=1):
            if lineno in breakpoints:
                trace.append(("breakpoint", lineno, line.strip()))
            match = re.search(r'debug_print\("([^"]+)",\s*([A-Za-z_]\w*)\)', line)
            if match:
                label, var = match.group(1), match.group(2)
                value = inputs.get(var, var)
                trace.append(("print", lineno, self.debug_print(label, value)))
        return trace
