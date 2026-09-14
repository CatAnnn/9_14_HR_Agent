# MinerU 图片与复杂版面预处理

知识库运行时仍只读取已处理的 Markdown 和受支持的组织数据表。PDF 的图片理解在入库前显式执行，避免应用启动时重复解析原始文档。

## 处理链路

1. MinerU 负责正文、公式、基础表格和内嵌图片解析。
2. Poppler 从 PDF 原生文字层提取文字与坐标，逐页比较 MinerU 的词项覆盖率和结构信号。
3. 对缺失明显的页面，只补入 MinerU 未保留的原生文字行；已有内容不会整页重复写入。
4. 最多选择风险最高的 3 页，且不超过文档页数的约 10%，以 180 DPI 渲染整页。
5. 视觉模型只分析这些候选页，输出可核验的版式、节点、关系、流程顺序、表格、证据和不确定项。
6. 原生文字补充和视觉关系补充写入最终 Markdown，再由现有 processed-data-only 索引流程处理。

原生文字层负责“字是什么”，视觉模型负责“对象之间是什么关系”。即使 OCR 已识别全部标签，大面积图形、流程关系和扫描页仍可进入候选，避免只保留文字而丢失箭头、包含关系和顺序。视觉分析失败时默认保留 MinerU 与原生文字结果，不会丢弃整份文档。

## 使用方式

在 backend 依赖和 MinerU 服务可用的环境中运行：

```bash
python scripts/prepare_visual_document.py \
  data/source/example.pdf \
  --output data/kb_raw/general/example.md
```

可选参数：

- `--skip-vision`：只关闭候选页渲染和下游整页视觉模型；MinerU 本身仍按其 backend/effort 配置运行。
- `--strict-vision`：任一候选页准备或分析失败即返回失败，适合发布前质量门禁。
- `--force`：覆盖已经存在的目标 Markdown。

命令会输出页数、补充块数量、视觉候选页、视觉分析统计和降级警告。MinerU 中间产物、质量报告以及候选页渲染图保存在配置的处理目录中，并按源文件内容和解析配置建立缓存。`--skip-vision` 使用独立缓存配置，只做 MinerU 与原生布局恢复，不生成整页图片。

正常视觉模式下若候选页没有全部渲染，本次解析不会写入完成清单，下次会重新尝试。最终 Markdown 使用同目录临时文件原子发布，避免中断后留下半份知识文档。

## 依赖与降级

backend 镜像需要 `poppler-utils`，其中：

- `pdftotext -bbox-layout` 用于原生文字与坐标提取；
- `pdftoppm` 用于候选页整页渲染。

Poppler 缺失、PDF 无文字层、单页渲染超时或视觉服务暂时不可用时，默认记录警告并继续返回已有解析结果。扫描 PDF 没有原生文字时不会伪造文字，只能依靠候选页视觉分析。

## 结果标记

最终 Markdown 使用两个稳定标记防止重复追加：

- `pdf-native-layout-fallback:v1`：原生文字层补回的缺失内容；
- `document-vision-layout-relations:v2`：复杂页视觉结构和关系。

修改阈值、渲染分辨率或视觉结构协议时，应同步升级缓存/标记版本并重新生成目标 Markdown。
