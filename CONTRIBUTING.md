# 贡献指南

## 环境

需要 Python 3.10 或更高版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

跟踪命令和 Word 日报使用上面的安装即可。只有在需要 LightGBM 方向预测、且不接受内置逻辑回归回退时，再安装可选依赖：

```bash
pip install -e ".[forecast]"
```

## 测试

```bash
python3 -m unittest discover -s tests
```

`tests/` 使用模拟响应，可以离线运行，也不读取本机私有目录。GitHub Actions 跑的是这同一套完整测试，并且不会安装 LightGBM。

## 修改范围

- 不要提交 `data/`、`logs/`、`outputs/`、SQLite 数据库或生成的日报。
- 新闻筛选、价格匹配和归因规则需要对应测试；没有 Issue 说明时，不要顺手改这些规则。
- 文档沿用现有中文即可。

## 反馈

- 缺陷：使用 Bug 报告模板。
- 功能：使用功能建议模板。
- 漏洞、凭据或可被滥用的抓取问题：见 [SECURITY.md](SECURITY.md)，不要开公开 Issue。
