"""零依赖 multipart/form-data 解析（仅用于本地内测的文件上传）。

不引入 python-multipart：直接按 RFC 2046 boundary 切分原始 body，抽取
- 文件部分：filename + content-type + 二进制内容；
- 普通表单字段：name -> 文本值。

仅做最小健壮处理（binary、不递归、限制大小），满足内网受控内测；
真实生产建议在反向网关层处理上传或安装受维护的 multipart 库。
"""
from __future__ import annotations


class MultipartError(ValueError):
    pass


def _split_header_params(line: str) -> tuple[str, dict]:
    parts = line.split(";")
    main = parts[0].strip().lower()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.strip().split("=", 1)
            params[k.strip().lower()] = v.strip().strip('"')
    return main, params


def parse_multipart(body: bytes, content_type: str, *,
                    max_size: int = 25 * 1024 * 1024) -> dict:
    """返回 {"fields": {name: str}, "files": [{name, filename, content_type, data}]}。"""
    if not content_type or "boundary=" not in content_type:
        raise MultipartError("缺少 multipart boundary")
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"')
    if len(body) > max_size:
        raise MultipartError("上传文件过大")
    delim = b"--" + boundary.encode("utf-8")
    fields: dict[str, str] = {}
    files: list[dict] = []

    # 以 boundary 切分
    parts = body.split(delim)
    for part in parts:
        # 去掉前导 CRLF / 结尾
        if part in (b"", b"--", b"--\r\n", b"\r\n", b"--\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if b"\r\n\r\n" not in part:
            continue
        header_blob, _, data = part.partition(b"\r\n\r\n")
        headers = {}
        name = None
        filename = None
        part_ct = "application/octet-stream"
        for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
            if ":" not in line:
                continue
            hname, hval = line.split(":", 1)
            hname = hname.strip().lower()
            hval = hval.strip()
            if hname == "content-disposition":
                _, params = _split_header_params(hval)
                name = params.get("name")
                filename = params.get("filename")
            elif hname == "content-type":
                part_ct = hval.split(";", 1)[0].strip()
            headers[hname] = hval
        if name is None:
            continue
        if filename is not None:
            files.append({"name": name, "filename": filename,
                          "content_type": part_ct, "data": data})
        else:
            fields[name] = data.decode("utf-8", errors="replace")
    return {"fields": fields, "files": files}
