# 考勤跨夜班与加班分段核算

面向制造企业薪酬核算员，处理采信、判定、核算、复核与归档。

## 启动

```bash
docker compose up --build -d
```

访问 http://localhost:18252 。初次启动自动运行脏样例，可切换 clean.csv 与 legacy.csv。顶部“运行批次”会重新计算，同源输入复用批次。

## 业务模块

- 打卡采信：`/m1_ingest`、`/api/m1_ingest`
- 班次日历：`/m2_mapping`、`/api/m2_mapping`
- 跨夜配对：`/m3_versions`、`/api/m3_versions`
- 缺卡复核：`/m4_segments`、`/api/m4_segments`
- 工时切段：`/m5_calculate`、`/api/m5_calculate`
- 薪资对账：`/m6_reconcile`、`/api/m6_reconcile`
- 周期归档：`/m7_archive`、`/api/m7_archive`

## 核心规则与裁决

跨夜按班次归属日配对，休息区间先扣除；工资倍率**按实际日历日分段**，片段仍保留 `shift_day`/`shift_id` 对应原班次。整班套用归属日费率仅保留为对比口径。同员工同一实际日历日的工作共用 8 小时正常工时额度。完整数值、边界和异常定义见 `docs/DATA-DICTIONARY.md`。

补卡、休息、假期或口径变更走 `POST /api/runs/<id>/recalculate`（口径快捷入口 `POST /api/runs/<id>/basis`）：候选结果通过自检才追加修订并生效，失败保留旧版；原始补卡记录与复核决定只追加不覆盖，可按修订号回看导出证据。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

原始样例在 `sample_data/`。测试在 `tests/`。封存文件是单份CSV，含运行清单、计价口径、计算证据、补卡证据、隔离原因与对账。复核需提交当前版本，陈旧复核返回409。

## 数据与限制

SQLite保存在records命名卷，普通重启与构建不会清除。确认可丢弃本项目数据后，运行 `docker compose down -v` 再启动可重置。单文件上限2MB；不接入外部业务系统；历史规则由本项目样例定义，不代表实际行业标准。
