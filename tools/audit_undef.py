#!/usr/bin/env python3
"""AST 静态检查：找出函数内「引用了但疑似未定义」的变量。

针对本次 bug 类型（引用作用域外变量 / 拼写错的局部变量）。
思路：对每个函数，收集 定义名（参数/赋值/import/for/with/comprehension/
全局声明/内建），再遍历 Load 上下文的 Name，报告不在集合中且不在
模块级/内建/导入中的名字。

注意：这是启发式工具，可能有误报，仅用于人工复查候选。
"""
from __future__ import annotations

import ast
import builtins
import os
import sys

BUILTINS = set(dir(builtins))

# 常见注入名（GTK 回调、框架注入等）白名单，避免噪音
WHITELIST = {
    "self", "cls", "args", "kwargs", "__name__", "__file__", "__doc__",
    "True", "False", "None",
}


def collect_bound_names(node: ast.AST) -> set[str]:
    """收集一个函数体内所有「被绑定」的名字。"""
    bound: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Name(self, n: ast.Name):
            if isinstance(n.ctx, (ast.Store, ast.Del)):
                bound.add(n.id)
            self.generic_visit(n)

        def visit_arg(self, n: ast.arg):
            bound.add(n.arg)

        def visit_Import(self, n: ast.Import):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])

        def visit_ImportFrom(self, n: ast.ImportFrom):
            for a in n.names:
                if a.name == "*":
                    continue
                bound.add(a.asname or a.name)

        def visit_Global(self, n: ast.Global):
            bound.update(n.names)

        def visit_Nonlocal(self, n: ast.Nonlocal):
            bound.update(n.names)

        def visit_ExceptHandler(self, n: ast.ExceptHandler):
            if n.name:
                bound.add(n.name)
            self.generic_visit(n)

        def visit_FunctionDef(self, n: ast.FunctionDef):
            # 嵌套函数名本身算绑定，但不下钻其内部（各自独立分析）
            bound.add(n.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, n: ast.ClassDef):
            bound.add(n.name)

        def visit_Lambda(self, n: ast.Lambda):
            for a in n.args.args:
                bound.add(a.arg)
            # 不下钻 lambda 体之外的细节

    Visitor().visit(node)
    return bound


def collect_loaded_names(node: ast.AST) -> list[tuple[str, int]]:
    """收集函数体内所有 Load 上下文的名字（含行号）。"""
    out: list[tuple[str, int]] = []

    class Visitor(ast.NodeVisitor):
        def visit_Name(self, n: ast.Name):
            if isinstance(n.ctx, ast.Load):
                out.append((n.id, n.lineno))

        def visit_FunctionDef(self, n: ast.FunctionDef):
            # 不下钻嵌套函数（它自己会被单独分析），但其默认值/装饰器算外层
            for d in n.decorator_list:
                self.visit(d)
            for dv in list(n.args.defaults) + [x for x in n.args.kw_defaults if x]:
                self.visit(dv)

        visit_AsyncFunctionDef = visit_FunctionDef

    Visitor().visit(node)
    return out


def analyze_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fp:
        try:
            tree = ast.parse(fp.read(), filename=path)
        except SyntaxError as e:
            return [f"{path}: 语法错误 {e}"]

    # 模块级绑定 + 所有顶层定义
    module_bound = collect_bound_names(tree)
    module_bound |= BUILTINS | WHITELIST

    findings: list[str] = []

    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound = collect_bound_names(func) | module_bound
        # 函数自身名（递归调用）
        bound.add(func.name)
        for name, lineno in collect_loaded_names(func):
            if name not in bound:
                findings.append(
                    f"{path}:{lineno}: 函数 {func.name}() 引用了疑似未定义的名字 `{name}`"
                )
    return findings


def main(argv: list[str]) -> int:
    roots = argv[1:] or ["."]
    skip = {"__pycache__", "target", "vendor", "backups", ".git", "tools/camilladsp-src"}
    all_findings: list[str] = []
    for root in roots:
        if os.path.isfile(root):
            files = [root]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in skip]
                for fn in filenames:
                    if fn.endswith(".py"):
                        files.append(os.path.join(dirpath, fn))
        for f in files:
            all_findings.extend(analyze_file(f))

    for line in all_findings:
        print(line)
    print(f"\n共 {len(all_findings)} 处疑似未定义引用")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
