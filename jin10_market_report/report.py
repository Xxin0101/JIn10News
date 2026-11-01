from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from .models import ProcessedNews, RunStats
from .processor import keyword_counter


ACCENT_BLUE = RGBColor(0x2E, 0x74, 0xB5)
DARK_BLUE = RGBColor(0x1F, 0x4D, 0x78)
MUTED = RGBColor(0x66, 0x66, 0x66)
RISK_RED = RGBColor(0x9B, 0x1C, 0x1C)
HEADER_FILL = "E8EEF5"


def _set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def _set_cell_width(cell, width_dxa: int) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.first_child_found_in("w:tcW")
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(width_dxa))
    tc_w.set(qn("w:type"), "dxa")


def _set_table_geometry(table, widths_dxa: list[int]) -> None:
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    tbl = table._tbl
    tbl_pr = tbl.tblPr
    tbl_w = tbl_pr.first_child_found_in("w:tblW")
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(sum(widths_dxa)))
    tbl_w.set(qn("w:type"), "dxa")

    tbl_ind = tbl_pr.first_child_found_in("w:tblInd")
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:w"), "120")
    tbl_ind.set(qn("w:type"), "dxa")

    grid = tbl.tblGrid
    if grid is None:
        grid = OxmlElement("w:tblGrid")
        tbl.insert(0, grid)
    for child in list(grid):
        grid.remove(child)
    for width in widths_dxa:
        col = OxmlElement("w:gridCol")
        col.set(qn("w:w"), str(width))
        grid.append(col)

    for row in table.rows:
        for idx, cell in enumerate(row.cells):
            _set_cell_width(cell, widths_dxa[min(idx, len(widths_dxa) - 1)])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def _format_run(run, bold: bool = False, color: RGBColor | None = None, size: int | None = None) -> None:
    run.bold = bold
    run.font.name = "Calibri"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    if color:
        run.font.color.rgb = color
    if size:
        run.font.size = Pt(size)


def _apply_styles(doc: Document) -> None:
    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = "Calibri"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.25

    for name, size, color, before, after in [
        ("Heading 1", 16, ACCENT_BLUE, 18, 10),
        ("Heading 2", 13, ACCENT_BLUE, 14, 7),
        ("Heading 3", 12, DARK_BLUE, 10, 5),
    ]:
        style = styles[name]
        style.font.name = "Calibri"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.font.size = Pt(size)
        style.font.color.rgb = color
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.line_spacing = 1.25


def _add_label_paragraph(doc: Document, label: str, value: str = ""):
    paragraph = doc.add_paragraph()
    label_run = paragraph.add_run(f"{label}: ")
    _format_run(label_run, bold=True, color=DARK_BLUE)
    if value:
        value_run = paragraph.add_run(value)
        _format_run(value_run)
    return paragraph


def _add_hyperlink(paragraph, text: str, url: str) -> None:
    part = paragraph.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    r_pr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    r_pr.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    r_pr.append(underline)
    new_run.append(r_pr)
    text_element = OxmlElement("w:t")
    text_element.text = text
    new_run.append(text_element)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def _add_summary_table(doc: Document, stats: RunStats, items: list[ProcessedNews]) -> None:
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    _set_table_geometry(table, [2340, 2340, 2340, 2340])
    headers = ["抓取总条数", "筛选后条数", "重复合并", "High Priority"]
    values = [
        str(stats.within_window_total),
        str(len(items)),
        str(stats.duplicate_merged_total),
        str(sum(1 for item in items if item.high_priority)),
    ]
    for idx, cell in enumerate(table.rows[0].cells):
        _set_cell_shading(cell, HEADER_FILL)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(f"{headers[idx]}\n{values[idx]}")
        _format_run(run, bold=True, color=DARK_BLUE)


def _add_keyword_overview(doc: Document, items: list[ProcessedNews]) -> None:
    if not items:
        return
    counter = keyword_counter(items)
    top = "、".join(f"{word}({count})" for word, count in counter.most_common(15))
    _add_label_paragraph(doc, "高频关键词", top)


def _add_bullet(doc: Document, label: str, value: str) -> None:
    paragraph = doc.add_paragraph(style=None)
    paragraph.paragraph_format.left_indent = Inches(0.375)
    paragraph.paragraph_format.first_line_indent = Inches(-0.188)
    paragraph.paragraph_format.space_after = Pt(4)
    run = paragraph.add_run("• ")
    _format_run(run, bold=True, color=DARK_BLUE)
    label_run = paragraph.add_run(f"{label}: ")
    _format_run(label_run, bold=True, color=DARK_BLUE)
    value_run = paragraph.add_run(value)
    _format_run(value_run)


def _add_item(doc: Document, item: ProcessedNews) -> None:
    heading = item.title_or_text
    prefix = "[High Priority] " if item.high_priority else ""
    p = doc.add_heading(f"{prefix}{item.raw_time}  {heading}", level=2)
    if item.high_priority:
        for run in p.runs:
            _format_run(run, bold=True, color=RISK_RED, size=13)

    _add_label_paragraph(doc, "发布时间", item.raw_time)
    _add_label_paragraph(doc, "信息来源", "金十数据")
    _add_label_paragraph(doc, "新闻标题或实时快讯原文", item.title_or_text)
    link_p = _add_label_paragraph(doc, "原文链接")
    _add_hyperlink(link_p, item.jin10_url, item.jin10_url)
    _add_label_paragraph(doc, "关键词", "、".join(item.keywords) if item.keywords else "无")
    _add_label_paragraph(doc, "事件分类", "、".join(item.categories))
    if item.merged_ids:
        _add_label_paragraph(doc, "重复合并", f"已合并 {len(item.merged_ids)} 条相似消息")
    _add_label_paragraph(doc, "中文简要摘要", item.summary)
    impact_heading = doc.add_paragraph()
    run = impact_heading.add_run("潜在市场影响分析")
    _format_run(run, bold=True, color=DARK_BLUE)
    _add_bullet(doc, "原油/LPG/LNG/天然气供应", item.impact_energy)
    _add_bullet(doc, "航线/运费/船期/出口节奏", item.impact_shipping)
    _add_bullet(doc, "市场价格/贸易流向", item.impact_price_trade)
    original = _add_label_paragraph(doc, "原始快讯")
    original.add_run(item.original_text)


def build_docx_report(
    items: list[ProcessedNews],
    stats: RunStats,
    output_path: Path,
    timezone: str = "Asia/Shanghai",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(timezone)
    generated_at = datetime.now(tz)

    doc = Document()
    section = doc.sections[0]
    section.start_type = WD_SECTION_START.NEW_PAGE
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1.0)
    section.right_margin = Inches(1.0)
    section.bottom_margin = Inches(1.0)
    section.left_margin = Inches(1.0)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)
    _apply_styles(doc)

    title = doc.add_paragraph()
    title.paragraph_format.space_after = Pt(3)
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = title.add_run("金十数据伊朗及中东能源贸易实时新闻日报")
    _format_run(run, bold=True, color=RGBColor(0x0B, 0x25, 0x45), size=20)

    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(10)
    subtitle_run = subtitle.add_run(
        f"生成时间: {generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
        f"时间范围: 过去 24 小时 | 数据源: 金十数据公开实时资讯"
    )
    _format_run(subtitle_run, color=MUTED, size=10)

    _add_summary_table(doc, stats, items)
    _add_keyword_overview(doc, items)

    doc.add_heading("日报正文", level=1)
    if not items:
        p = doc.add_paragraph("过去 24 小时内未筛选到符合主题规则的金十数据实时资讯。")
        _format_run(p.runs[0])
    for item in items:
        _add_item(doc, item)

    doc.save(output_path)
    return output_path
