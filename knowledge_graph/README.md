# VCB Trip 故障知识图谱

打开 `vcb_trip_knowledge_graph.html` 即可查看交互图谱，无需启动服务器。

图谱路径：`VCB Trip → 归一化信号表现 → 涉及设备 → 维修方法 → 来源工单`

- 案例：25
- 工单：16
- 节点：79
- 关系：301
- 工单源文件：`工单.xlsx`
- 结构化工单源：`fault_extractor\fault_extractor\output\reference_data_with_cause_inferenced.json`
- 案例库源：`case_library`

## 信号归一化规则

- 同一业务信号在不同车厢/设备单元中的编码合并为一个信号节点。
- 只有“信号名称”和“前值 → 后值”都相同时才合并。
- 已排除 `distance` 和 `BECU Speed`，它们不会出现在节点、关系和页面中。

## 设备和维修节点

- 图谱五列结构和连线方向不变，每个具体部件独立建点。
- 不根据共同出现关系合并 controller、receiver 或其他不同部件。
- 部件与维修动作的关系只来自同一工单中的结构化部件—动作配对。
- 维修节点界面只显示复位、检查、测试、更换、互换测试、隔离、修复等动作类型，不重复显示部件名称。
- 具体部件由设备节点展示；`member_methods`、`method_work_orders`、`affected_equipment` 和 `raw_actions` 仍保留具体维修动作与工单原文。
- 工单多选与整条线路点亮继续按照原工单证据计算。

## 文件

- `vcb_trip_knowledge_graph.html`：可直接查看和筛选的知识图谱页面。
- `vcb_trip_knowledge_graph.json`：包含节点、关系、工单证据的完整数据。
- `vcb_trip_nodes.csv`：节点表，可导入 Neo4j 等图数据库。
- `vcb_trip_edges.csv`：关系表，可导入 Neo4j 等图数据库。

## 关系含义

- `phenomenon_to_signal`：历史 VCB Trip 案例中观测到该归一化信号变化。
- `signal_to_equipment`：信号和设备出现在同一工单中，是证据关联而非单独证明因果。
- `equipment_to_maintenance`：工单记录对该设备执行了相应处置。
- `maintenance_to_work_order`：维修方法来源于对应工单。

## 导入 Neo4j

在项目目录的 PowerShell 中执行：

```powershell
py -3.13 -m pip install -r requirements-knowledge-graph.txt
py -3.13 import_vcb_knowledge_graph_to_neo4j.py
```

程序默认连接 `bolt://43.199.48.66:7687`，用户名为 `neo4j`，运行时会安全地询问密码。

如果 Neo4j 中已经导入过旧版图谱，请使用下面的命令替换旧版 VCB 图谱；它只清除 `graph_scope=vcb_trip` 的节点，不影响其他数据：

```powershell
py -3.13 import_vcb_knowledge_graph_to_neo4j.py --replace-existing
```
