"""监管接口 HTTP 客户端（标准库 urllib，零依赖）。

接口契约:
  POST {REGULATOR_URL}
  Header: X-Org-Code, Content-Type: application/json
  Body  : {"batch_ref": str, "records": [ {rx_no, patient_id, drug, quantity, fee}, ... ]}
  200   : {"results": [ {"rx_no":..., "accepted":bool, "regulator_id":str|null,
                          "error_code":str|null, "error_message":str|null,
                          "retriable":bool}, ... ]}   -- 顺序与入参一致
  其它  : 传输/批次级失败，调用方按可重试失败处理本 chunk 全部行
"""
import json
import urllib.error
import urllib.request

from . import config


class TransportError(Exception):
    """网络错误 / 非 200 响应（批次级、可重试）。"""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def post_prescriptions(records, batch_ref, url=None, timeout=None,
                       org_code=None, force_flaky=False, force_outage=False,
                       delay_ms=0):
    url = url or config.REGULATOR_URL
    timeout = config.REGULATOR_TIMEOUT if timeout is None else timeout
    headers = {
        "Content-Type": "application/json",
        "X-Org-Code": org_code or config.ORG_CODE,
    }
    # 仅用于演示：强制触发监管端瞬时故障 / 一次性宕机
    if force_flaky:
        headers["X-Mock-Flaky"] = "1"
    if force_outage:
        headers["X-Mock-Outage"] = "1"
    if delay_ms:
        headers["X-Mock-Delay-Ms"] = str(delay_ms)

    body = json.dumps({"batch_ref": batch_ref, "records": records}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise TransportError(f"监管接口返回 HTTP {e.code}", status=e.code,
                             body=e.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise TransportError(f"无法连接监管接口: {e.reason}")
    except (TimeoutError, OSError) as e:
        raise TransportError(f"监管接口请求失败: {e}")

    results = {r["rx_no"]: r for r in payload.get("results", [])}
    out = []
    for rec in records:
        r = results.get(rec["rx_no"])
        if r is None:
            # 监管端漏回该行：保守地按可重试失败处理，绝不静默当成功
            out.append({
                "rx_no": rec["rx_no"], "accepted": False, "regulator_id": None,
                "error_code": "E_NO_RESULT", "error_message": "监管端未返回该行结果",
                "retriable": True,
            })
        else:
            out.append(r)
    return out


def admin_reset(url=None):
    """重置监管端模拟器状态（测试/演示用）。"""
    url = (url or config.REGULATOR_URL).replace("/v1/prescriptions", "/admin/reset")
    req = urllib.request.Request(url, data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))
