"""
游戏内着色器编译与缓存系统 - 验收测试
运行方式：python -m pytest tests/test_shader_compiler.py -q
共 15 个测试用例
"""
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rendering.shader_compiler import ShaderCompiler, ShaderCompileError


class TestShaderCompiler:
    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.system = ShaderCompiler({"cache_dir": self.tmp.name})

    def teardown_method(self):
        self.tmp.cleanup()

    # ---- 1. 着色器编译：预处理（宏/包含）与优化 ---------------------
    def test_case_01(self):
        """预处理：#define 宏替换与 #ifdef 条件编译。"""
        src = "#define FOO 1\n#ifdef FOO\nv = FOO;\n#else\nv = 0;\n#endif"
        out = self.system.preprocess(src)
        assert "v = 1;" in out
        assert "v = 0;" not in out

    def test_case_02(self):
        """预处理：#include 通过 include_resolver 展开。"""
        headers = {"common.h": "float helper() { return 1.0; }"}
        src = '#include "common.h"\nfloat x = helper();'
        out = self.system.preprocess(src, include_resolver=headers.get)
        assert "float helper()" in out
        assert "float x = helper();" in out

    def test_case_03(self):
        """优化：去除注释/空行/多余空白，保留有效代码。"""
        src = "// comment\n/* block\ncomment */\n\nfloat   a   =   1.0;  // tail\n"
        opt = self.system.optimize(src)
        assert "//" not in opt and "block" not in opt
        assert opt == "float a = 1.0;"

    def test_case_04(self):
        """compile_shader 真实编译并产出预处理/优化后的代码。"""
        src = "#define V 2\n// comment\nint x = V;"
        result = self.system.compile_shader(src)
        assert result["from_cache"] is False
        assert result["code"] == "int x = 2;"
        assert result["hash"]

    # ---- 2. 着色器缓存：缓存命中、持久化与失效 -----------------------
    def test_case_05(self):
        """重复编译命中缓存。"""
        src = "float y = 1.0;"
        first = self.system.compile_shader(src)
        second = self.system.compile_shader(src)
        assert first["from_cache"] is False
        assert second["from_cache"] is True
        assert second["code"] == first["code"]

    def test_case_06(self):
        """缓存持久化：新实例从磁盘加载后仍命中缓存。"""
        src = "float z = 3.0;"
        self.system.compile_shader(src)
        other = ShaderCompiler({"cache_dir": self.tmp.name})
        assert other.cache_get(src) is not None
        assert other.compile_shader(src)["from_cache"] is True

    def test_case_07(self):
        """缓存失效：源码变化产生新键（旧缓存不误用），invalidate 可清除。"""
        src = "float w = 1.0;"
        self.system.compile_shader(src)
        changed = "float w = 2.0;"
        assert self.system.compile_shader(changed)["from_cache"] is False
        assert self.system.invalidate(changed) is True
        assert self.system.cache_get(changed) is None
        assert self.system.cache_size() == 1

    # ---- 3. 着色器变体：排列组合与裁剪 -------------------------------
    def test_case_08(self):
        """生成全部排列组合。"""
        space = {"A": [0, 1], "B": [0, 1], "C": [0, 1]}
        variants = self.system.generate_variants(space)
        assert len(variants) == 2 ** 3
        assert {"A": 1, "B": 0, "C": 1} in variants

    def test_case_09(self):
        """变体宏在编译中生效。"""
        src = "#if USE_FOG\ncolor += fog;\n#endif\nout = color;"
        space = {"USE_FOG": [0, 1]}
        compiled = self.system.compile_variants(src, space)
        assert "fog" in compiled["USE_FOG=1"]["code"]
        assert "fog" not in compiled["USE_FOG=0"]["code"]

    def test_case_10(self):
        """变体裁剪：谓词与禁止组合。"""
        space = {"A": [0, 1], "B": [0, 1]}
        variants = self.system.generate_variants(space)
        kept = self.system.strip_variants(
            variants,
            keep=lambda v: v["A"] == 1,
            forbid=[{"A": 1, "B": 1}],
        )
        assert kept == [{"A": 1, "B": 0}]

    # ---- 4. 着色器热重载：文件监视与状态保持 -------------------------
    def test_case_11(self):
        """文件监视：poll_changes 检测到着色器文件变化。"""
        path = os.path.join(self.tmp.name, "shader.glsl")
        with open(path, "w") as f:
            f.write("float t = 1.0;")
        self.system.watch_file(path)
        time.sleep(0.01)
        with open(path, "w") as f:
            f.write("float t = 2.0;")
        assert path in self.system.poll_changes()

    def test_case_12(self):
        """热重载：文件修改后重新编译为新代码。"""
        path = os.path.join(self.tmp.name, "shader2.glsl")
        with open(path, "w") as f:
            f.write("float t = 1.0;")
        first = self.system.hot_reload(path)
        with open(path, "w") as f:
            f.write("float t = 9.0;")
        second = self.system.hot_reload(path)
        assert first["code"] != second["code"]
        assert second["code"] == "float t = 9.0;"
        assert self.system.get_program(path)["code"] == "float t = 9.0;"

    def test_case_13(self):
        """热重载后渲染状态保持。"""
        path = os.path.join(self.tmp.name, "shader3.glsl")
        with open(path, "w") as f:
            f.write("float t = 1.0;")
        self.system.hot_reload(path)
        self.system.set_render_state(blend=True, texture="diffuse")
        with open(path, "w") as f:
            f.write("float t = 2.0;")
        self.system.hot_reload(path)
        state = self.system.get_render_state()
        assert state["blend"] is True and state["texture"] == "diffuse"

    # ---- 5. 着色器调试：打印与断点 -----------------------------------
    def test_case_14(self):
        """调试打印：debug_print 记录格式化信息，debug_run 捕获行内调用。"""
        self.system.debug_print("value=%d", 42)
        assert "value=42" in self.system.debug_log
        result = self.system.debug_run('debug_print("x=%s", x)\nfloat y = 0;')
        assert result["prints"] == ["x=x"]

    def test_case_15(self):
        """调试断点：命中断点暂停记录，支持条件断点与清除。"""
        src = "line1;\nline2;\nline3;"
        self.system.set_breakpoint(2)
        self.system.set_breakpoint(3, condition=lambda line: False)
        result = self.system.debug_run(src)
        assert result["hits"] == [(2, "line2;")]
        self.system.clear_breakpoint(2)
        assert self.system.debug_run(src)["hits"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
