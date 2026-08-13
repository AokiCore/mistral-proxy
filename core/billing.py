# -*- coding: utf-8 -*-
"""免费账号的月度美元额度查询。

实测依据：免费档发的是每月 10 美元 API 额度，不是 token 配额。花光后所有模型
一起返回 402 "Check your subscription"，每月 1 号零点 UTC 重置。同样 8000 万
token，打 mistral-embed 只花 $0.74，打 glm-5-2 就能把 $10 用满 —— 所以只看
token 数完全判断不出账号还能不能用。

这个数字 API key 读不到（/v1/* 下没有任何账务端点，全部 404），只有控制台后端有：
GET /api/billing/v2/budget，376 字节纯 JSON，需要控制台会话。

会话优先用注册脚本落库的那份（Kratos 签发，有效期 90 天），所以正常情况下网关
一次登录都不用做；只有会话过期或者老账号没存过，才拿密码换一份新的并写回库里。
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

KRATOS_LOGIN = "https://auth.mistral.ai/self-service/login/browser"
CONSOLE = "https://admin.mistral.ai"
BUDGET_API = "/api/billing/v2/budget"
BUDGET_PAGE = "/subscription"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# 控制台同时给 vibe_budget 和 api_budget，只有后者跟 API 调用有关
_BUDGET_KEY = '"api_budget":'


class BillingError(Exception):
    """额度查不到（登录失败、会话过期、页面改版）。"""


@dataclass(slots=True)
class Budget:
    used_pct: float = 0.0
    total: float = 0.0
    currency: str = "USD"
    reset_at: str = ""

    @property
    def exhausted(self) -> bool:
        return self.used_pct >= 100.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.total * (1.0 - self.used_pct / 100.0))

    def to_dict(self) -> dict:
        return {"used_pct": round(self.used_pct, 4), "total": self.total,
                "currency": self.currency, "reset_at": self.reset_at,
                "remaining": round(self.remaining, 4)}


def _decode(session: str) -> dict:
    try:
        d = json.loads(session or "{}")
    except json.JSONDecodeError:
        return {}
    return d if isinstance(d, dict) and d else {}


def next_reset_ts(reset_at: str = "") -> float:
    """额度重置时刻；给不出有效值时退回「下月 1 号零点 UTC」。"""
    if reset_at:
        try:
            return datetime.fromisoformat(reset_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    now = datetime.now(timezone.utc)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


def parse_budget(data: dict) -> Budget:
    """解析 /api/billing/v2/budget 的返回。"""
    d = (data or {}).get("api_budget")
    if not isinstance(d, dict):
        raise BillingError("返回里没有 api_budget")
    return Budget(used_pct=float(d.get("usage_percentage") or 0.0),
                  total=float(d.get("initial_budget") or 0.0),
                  currency=d.get("currency") or "USD",
                  reset_at=d.get("reset_at") or "")


def extract_api_budget(page: str) -> Budget:
    """兜底：接口不可用时从订阅页渲染载荷里抠 budget.api_budget。"""
    text = page.replace('\\"', '"')
    i = text.find(_BUDGET_KEY)
    if i < 0:
        raise BillingError("页面里找不到 api_budget（会话失效或页面改版）")
    start = i + len(_BUDGET_KEY)
    depth = 0
    for n in range(start, min(len(text), start + 4000)):
        ch = text[n]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    d = json.loads(text[start:n + 1])
                except json.JSONDecodeError as e:
                    raise BillingError(f"api_budget 解析失败：{e}") from e
                return Budget(used_pct=float(d.get("usage_percentage") or 0.0),
                              total=float(d.get("initial_budget") or 0.0),
                              currency=d.get("currency") or "USD",
                              reset_at=d.get("reset_at") or "")
    raise BillingError("api_budget 对象不完整")


def session_cookies(jar) -> dict:
    """只留身份 cookie；Cloudflare 那几个是短命噪音，存了反而误导。

    httpx.Cookies 直接迭代给出的是名字字符串，真正的 cookie 对象在 .jar 里。
    """
    return {c.name: c.value for c in getattr(jar, "jar", jar)
            if c.name.startswith("ory_session")}


class BudgetClient:
    """查额度。

    优先用注册时存下来的控制台会话（90 天有效），没有或已失效才拿密码登一次，
    并把新会话回传给调用方存起来。fetch 返回 (额度, 可用的会话 JSON)。
    """

    def __init__(self, timeout: float = 60.0):
        self._timeout = timeout

    async def fetch(self, email: str, password: str = "",
                    session: str = "") -> tuple[Budget, str]:
        cookies = _decode(session)
        if cookies:
            try:
                return await self._read(cookies), session
            except BillingError:
                pass          # 会话过期，下面用密码换一份
        if not password:
            raise BillingError("会话失效且没有控制台密码，查不了额度")
        jar = await self._login(email, password)
        budget = await self._read(jar)
        return budget, json.dumps(session_cookies(jar))

    async def _read(self, cookies) -> Budget:
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True,
                                     cookies=cookies, headers={"User-Agent": UA}) as c:
            r = await c.get(CONSOLE + BUDGET_API, headers={"Accept": "application/json"})
            if r.status_code == 200:
                try:
                    return parse_budget(r.json())
                except ValueError as e:
                    raise BillingError(f"额度接口返回不是 JSON：{e}") from e
            if r.status_code in (401, 403):
                raise BillingError(f"会话失效（{r.status_code}）")
            # 接口改路径了就退回去解析页面，别让整个功能一起挂掉
            page = await c.get(CONSOLE + BUDGET_PAGE, headers={"RSC": "1"})
            if page.status_code != 200:
                raise BillingError(f"额度接口 {r.status_code}，订阅页 {page.status_code}")
            return extract_api_budget(page.text)

    async def _login(self, email: str, password: str) -> httpx.Cookies:
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True,
                                     headers={"User-Agent": UA}) as c:
            try:
                flow = (await c.get(KRATOS_LOGIN,
                                    headers={"Accept": "application/json"})).json()
                action = flow["ui"]["action"]
                csrf = next((n["attributes"].get("value") for n in flow["ui"]["nodes"]
                             if (n.get("attributes") or {}).get("name") == "csrf_token"), "")
            except (httpx.HTTPError, KeyError, ValueError) as e:
                raise BillingError(f"拿不到登录流程：{e}") from e
            r = await c.post(action, headers={"Accept": "application/json"},
                             json={"method": "password", "identifier": email,
                                   "password": password, "csrf_token": csrf})
            if r.status_code != 200:
                raise BillingError(f"控制台登录失败 {r.status_code}")
            return c.cookies
