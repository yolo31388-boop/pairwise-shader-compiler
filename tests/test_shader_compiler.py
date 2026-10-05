"""
游戏内着色器编译与缓存系统 - 验收测试
运行方式：python -m pytest tests/test_shader_compiler.py -q
共 15 个测试用例
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rendering.shader_compiler import ShaderCompiler

SRC = "void main() { gl_FragColor = vec4(1.0); }"
SRC_V2 = "void main() { gl_FragColor = vec4(0.5); }"


class TestShaderCompiler:
    def setup_method(self):
        self.system = ShaderCompiler()

    # ---- 着色器编译 ----
    def test_case_01(self):
        """编译：可以从源码编译着色器，而不是只能用预编译产物"""
        shader = self.system.compile_shader(SRC, name="basic")
        assert shader.bytecode
        assert shader.source_hash
        assert shader.from_cache is False
        assert "gl_FragColor" in shader.source

    def test_case_02(self):
        """预处理：支持 #define 宏与 #ifdef 条件编译"""
        src = (
            "#define COLOR vec4(1.0)\n"
            "#ifdef USE_FOG\n"
            "float fog = 1.0;\n"
            "#endif\n"
            "vec4 c = COLOR;"
        )
        out = self.system.preprocess(src, defines={"USE_FOG": "1"})
        assert "#define" not in out
        assert "vec4 c = vec4(1.0);" in out
        assert "fog" in out
        out2 = self.system.preprocess(src)
        assert "fog" not in out2

    def test_case_03(self):
        """预处理：支持 #include 包含展开"""
        includes = {"common.glsl": "float pi() { return 3.14159; }"}
        src = '#include "common.glsl"\nvoid main() { float p = pi(); }'
        out = self.system.preprocess(src, includes=includes)
        assert "3.14159" in out
        assert "#include" not in out

    def test_case_04(self):
        """优化：去除注释空行并化简常量表达式"""
        src = (
            "// 这是注释\n"
            "/* 块注释 */\n"
            "vec4 c = color * 1.0 + 0.0;\n"
            "\n"
        )
        out = self.system.optimize(src)
        assert "注释" not in out
        assert "* 1.0" not in out
        assert "+ 0.0" not in out
        assert "vec4 c = color;" in out

    # ---- 着色器缓存 ----
    def test_case_05(self):
        """缓存：编译结果被缓存，重复编译直接命中"""
        first = self.system.compile_shader(SRC)
        second = self.system.compile_shader(SRC)
        assert first.from_cache is False
        assert second.from_cache is True
        assert first.bytecode == second.bytecode
        assert self.system.cache_size() == 1

    def test_case_06(self, tmp_path):
        """缓存：持久化到磁盘，重启（新实例）后缓存不丢失"""
        cache_dir = str(tmp_path / "cache")
        c1 = ShaderCompiler({"cache_dir": cache_dir})
        c1.compile_shader(SRC)
        c1.save_cache()
        assert os.path.exists(os.path.join(cache_dir, "shader_cache.json"))

        c2 = ShaderCompiler({"cache_dir": cache_dir})
        shader = c2.compile_shader(SRC)
        assert shader.from_cache is True

    def test_case_07(self):
        """缓存：源码变更后旧缓存失效"""
        s1 = self.system.compile_shader(SRC)
        assert self.system.is_cached(SRC)
        assert self.system.invalidate(SRC) is True
        assert not self.system.is_cached(SRC)
        s2 = self.system.compile_shader(SRC)
        assert s2.from_cache is False
        # 源码修改后哈希不同，不会误命中旧缓存
        s3 = self.system.compile_shader(SRC_V2)
        assert s3.bytecode != s1.bytecode

    # ---- 着色器变体 ----
    def test_case_08(self):
        """变体：生成关键字取值的全部排列组合"""
        variants = self.system.generate_variants(
            {"LIGHT": ["ON", "OFF"], "SHADOW": ["ON", "OFF"], "FOG": ["ON", "OFF"]}
        )
        assert len(variants) == 8
        assert {"LIGHT": "ON", "SHADOW": "OFF", "FOG": "ON"} in variants
        assert {"LIGHT": "OFF", "SHADOW": "OFF", "FOG": "OFF"} in variants

    def test_case_09(self):
        """变体：裁剪掉未使用的变体"""
        variants = self.system.generate_variants(
            {"LIGHT": ["ON", "OFF"], "SHADOW": ["ON", "OFF"]}
        )
        used = [{"LIGHT": "ON", "SHADOW": "ON"}]
        stripped = self.system.strip_variants(variants, used)
        assert stripped == used
        compiled = self.system.compile_variants("main", SRC,
                                                {"LIGHT": ["ON", "OFF"],
                                                 "SHADOW": ["ON", "OFF"]},
                                                used=used)
        assert len(compiled) == 1
        assert len(self.system.get_variants("main")) == 1

    # ---- 着色器热重载 ----
    def test_case_10(self, tmp_path):
        """热重载：运行时修改着色器文件后自动重新编译"""
        path = str(tmp_path / "shader.glsl")
        with open(path, "w") as f:
            f.write(SRC)
        reloaded_shaders = []
        self.system.watch_file(path, callback=lambda s: reloaded_shaders.append(s))
        self.system.compile_shader(SRC, name="shader.glsl")

        with open(path, "w") as f:
            f.write(SRC_V2)
        future = time.time() + 2
        os.utime(path, (future, future))

        reloaded = self.system.check_updates()
        assert path in reloaded
        assert len(reloaded_shaders) == 1
        assert reloaded_shaders[0].source == SRC_V2
        assert reloaded_shaders[0].from_cache is False

    def test_case_11(self, tmp_path):
        """热重载：文件监视能区分变化与未变化"""
        path = str(tmp_path / "watched.glsl")
        with open(path, "w") as f:
            f.write(SRC)
        self.system.watch_file(path)
        assert self.system.check_updates() == []

        with open(path, "w") as f:
            f.write(SRC_V2)
        future = time.time() + 2
        os.utime(path, (future, future))
        assert self.system.check_updates() == [path]
        # 重载后再次轮询不应重复触发
        assert self.system.check_updates() == []

    def test_case_12(self, tmp_path):
        """热重载：重载后渲染状态（uniforms）不丢失"""
        path = str(tmp_path / "stateful.glsl")
        with open(path, "w") as f:
            f.write(SRC)
        self.system.watch_file(path)
        self.system.set_uniform("u_time", 1.5)
        self.system.set_uniform("u_resolution", (1920, 1080))

        with open(path, "w") as f:
            f.write(SRC_V2)
        future = time.time() + 2
        os.utime(path, (future, future))
        self.system.check_updates()

        assert self.system.get_uniform("u_time") == 1.5
        assert self.system.get_uniform("u_resolution") == (1920, 1080)

    # ---- 着色器调试 ----
    def test_case_13(self):
        """调试：调试模式下编译产物带调试信息"""
        self.system.enable_debug(True)
        shader = self.system.compile_shader(SRC, name="dbg")
        assert shader.debug_info is not None
        assert shader.debug_info["line_count"] >= 1
        assert "gl_FragColor" in shader.debug_info["lines"][0]

    def test_case_14(self):
        """调试：注入打印并在执行时输出调试信息"""
        src = "vec4 c = vec4(1.0);"
        injected = self.system.inject_debug_print(src, "c")
        assert 'debug_print("c", c);' in injected

        shader = self.system.compile_shader(injected, optimize=False, name="prt")
        trace = self.system.debug_run(shader, inputs={"c": "vec4(1.0)"})
        prints = [t for t in trace if t[0] == "print"]
        assert len(prints) == 1
        assert "[DEBUG] c = vec4(1.0)" in self.system.get_debug_log()

    def test_case_15(self):
        """调试：断点在指定行触发"""
        src = "float a = 1.0;\nfloat b = 2.0;\nfloat c = a + b;"
        shader = self.system.compile_shader(src, optimize=False, name="bp")
        self.system.set_breakpoint("bp", 2)
        trace = self.system.debug_run(shader)
        hits = [t for t in trace if t[0] == "breakpoint"]
        assert len(hits) == 1
        assert hits[0][1] == 2
        assert "float b = 2.0;" in hits[0][2]
        self.system.clear_breakpoint("bp", 2)
        assert [t for t in self.system.debug_run(shader) if t[0] == "breakpoint"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
