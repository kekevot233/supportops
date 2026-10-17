# 开发与测试

请从仓库根目录运行，使用 Python 3.12、`requirements-ci.txt` 和 `pip install -e .`。
默认保持 Fake、Hashing 与临时数据库；不自动下载模型、不读真实配置、不发邮件。

```powershell
$env:PYTHON_DOTENV_DISABLED='1'
$env:HELLO_AGENTS_RUN_REAL_STORES='0'
python -m unittest test_release_preparation -v
python -m unittest discover -p "test_supportops*.py" -v
python -m supportops evaluate --output outputs/dev_retrieval.json
```

按改动选择受影响的测试模块，不要求每次全量回归。修改公共框架接口、数据库迁移或重要发布时，
安装 `requirements-legacy-test.txt` 后再显式运行 `python -m unittest discover -p "test_*.py" -v`。
四项真实外部存储测试默认跳过。CI 不进行真实模型、存储服务或浏览器测试。

`evaluate-answers` 的 Fake 基线预期为 20/32、退出码 1；这是已知质量不足，不应改 gold、阈值或
题目来制造全绿。单元测试通过不代表模型答案正确。

提交前运行 `python scripts/check_release.py`，人工检查清单和暂存差异。不要 `git add .` 后直接
提交；已有暂存文件不会因新增 `.gitignore` 自动移除。不要把旧暂存内容覆盖为自认为正确的版本。
新代码请记录来源、使用 Fake 注入，且不得将文档中的指令当作用户授权。
