import zipfile

from aibrain.extractors import extract_text

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def make_zip(path, files: dict):
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return path


def make_pdf(path, text: str):
    stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 144] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(out)
    return path


def test_pdf(tmp_path):
    text = extract_text(make_pdf(tmp_path / "doc.pdf", "Hard pity is 90 pulls"))
    assert text.startswith("## Page 1") and "Hard pity is 90 pulls" in text


def test_pdf_without_text_and_corrupt_files(tmp_path):
    assert extract_text(make_pdf(tmp_path / "blank.pdf", "")) is None  # e.g. a scan that needs OCR
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 garbage")
    assert extract_text(tmp_path / "broken.pdf") is None
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    assert extract_text(tmp_path / "broken.docx") is None


def test_docx(tmp_path):
    doc = f"""<w:document {W}><w:body>
      <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Combat</w:t></w:r></w:p>
      <w:p><w:r><w:t xml:space="preserve">Auto battle with </w:t></w:r><w:r><w:t>manual skills.</w:t></w:r></w:p>
      <w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>Combo meter</w:t></w:r></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>SSR</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>3%</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:body></w:document>"""
    text = extract_text(make_zip(tmp_path / "gdd.docx", {"word/document.xml": doc}))
    assert text == "# Combat\n\nAuto battle with manual skills.\n\n- Combo meter\n\n| SSR | 3% |\n"


def test_pptx_orders_slides_numerically(tmp_path):
    def slide(*lines):
        return f"<p:sld {A} xmlns:p='p'>" + "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in lines) + "</p:sld>"
    files = {"ppt/slides/slide1.xml": slide("Roadmap", "Q1: combat"),
             "ppt/slides/slide2.xml": slide("Gacha"),
             "ppt/slides/slide10.xml": slide("Thanks"),
             "ppt/notesSlides/notesSlide1.xml": slide("Mention the beta date", "1")}
    text = extract_text(make_zip(tmp_path / "deck.pptx", files))
    assert text.index("## Slide 1") < text.index("Gacha") < text.index("Thanks")
    assert "Roadmap\n\nQ1: combat\n\nNotes: Mention the beta date" in text


def test_xlsx(tmp_path):
    files = {
        "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="Rates" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                      '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": f"<sst {S}><si><t>Rarity</t></si><si><t>Rate</t></si><si><t>SSR</t></si></sst>",
        "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData>'
                                    '<row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
                                    '<row><c t="s"><v>2</v></c><c><v>0.03</v></c></row>'
                                    '<row><c t="inlineStr"><is><t>Note</t></is></c></row>'
                                    '</sheetData></worksheet>',
    }
    text = extract_text(make_zip(tmp_path / "rates.xlsx", files))
    assert text == "## Sheet: Rates\n\n| Rarity | Rate |\n| SSR | 0.03 |\n| Note |\n"


def test_html(tmp_path):
    (tmp_path / "page.html").write_text(
        "<html><head><style>body{}</style><script>alert(1)</script></head><body>"
        "<h1>Patch notes</h1><p>New  hero:   Kasel</p><ul><li>Buffs</li></ul></body></html>")
    text = extract_text(tmp_path / "page.html")
    assert text == "# Patch notes\n\nNew hero: Kasel\n\n- Buffs\n"


def test_plain_text_and_unknown(tmp_path):
    (tmp_path / "a.md").write_text("# Hi\n")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01")
    assert extract_text(tmp_path / "a.md") == "# Hi\n"
    assert extract_text(tmp_path / "b.bin") is None
