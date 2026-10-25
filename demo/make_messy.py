"""Build a realistic messy Downloads folder for the demo. Usage: python demo/make_messy.py DIR"""

import io
import struct
import sys
import zipfile
import zlib
from pathlib import Path


def tiny_pdf(text: str) -> bytes:
    """A minimal one-page PDF with real extractable text, no dependencies."""
    lines = text.split("\n")
    ops = ["BT /F1 12 Tf 72 720 Td 14 TL"] + [f"({l}) '" for l in lines] + ["ET"]
    stream = "\n".join(ops).encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def tiny_png(w=4, h=4) -> bytes:
    raw = b"".join(b"\x00" + b"\x80\xc0\xff" * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


FILES = {
    "download (7).pdf": tiny_pdf(
        "INVOICE  #INV-20431\nHetzner Online GmbH\nBilling period: September 2026\n"
        "Cloud Server CX22   EUR 4.51\nTotal due: EUR 4.51"),
    "scan0001.pdf": tiny_pdf(
        "Lecture 7: Public Key Cryptography\nCS 342 Network Security\n"
        "RSA key generation, Diffie-Hellman exchange, digital signatures"),
    "untitled.py": (
        "import socket\nimport sys\n\n# quick TCP port scanner for my homelab\n"
        "def scan(host, ports):\n    for p in ports:\n        s = socket.socket()\n"
        "        s.settimeout(0.5)\n        if s.connect_ex((host, p)) == 0:\n"
        "            print(f'open {p}')\n        s.close()\n"),
    "New Text Document.txt": (
        "groceries: oats, bananas, peanut butter, instant coffee\n"
        "also return library book by friday"),
    "notes_final_FINAL(2).md": (
        "# GSoC proposal ideas\n- fuzzing harness for a parser library\n"
        "- improve CLI error messages\n- mentor call on Tuesday"),
    "export.csv": (
        "date,amount,merchant\n2026-10-01,249.00,Swiggy\n2026-10-03,1200.00,Airtel\n"
        "2026-10-07,89.00,Uber\n"),
    "data.json": '{"ctf": "picoCTF", "solved": ["web/cookies", "crypto/rsa-oracle"], "score": 1450}',
    "Screenshot 2026-10-21 at 23.14.07.png": tiny_png(),
    "IMG_20261019_181233.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 64,
    "archive.zip": None,  # filled below
    "code_1.90.2-1718751586_amd64.deb": b"!<arch>\n" + b"\x00" * 64,
    "page.html": (
        "<html><head><title>Ollama API docs - generate a chat completion</title></head>"
        "<body><h1>POST /api/chat</h1></body></html>"),
}


def main(target: str) -> None:
    root = Path(target)
    root.mkdir(parents=True, exist_ok=True)
    for name, content in FILES.items():
        p = root / name
        if name == "archive.zip":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("wallpapers/forest.txt", "placeholder")
            content = buf.getvalue()
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
    print(f"made {len(FILES)} messy files in {root}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "messy-downloads")
