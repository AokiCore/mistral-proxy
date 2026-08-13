# -*- coding: utf-8 -*-
"""免费账号额度耗尽后，靠「删组织→建组织→发新 key」刷新额度。

实测依据：Mistral 免费档的每月 10 美元额度绑定在「组织」上，不是绑定账号或
workspace。把整个组织删掉、再建一个新的，新组织会带一个全新的 default workspace
和满血的 10 美元额度（api_budget.usage_percentage 立刻归零，不用等月初重置）。
用户账号本身不受影响，console_session 仍然有效。

端点（全部需要 ory_session_* cookie + X-Csrftoken header，csrftoken 从 cookie 取）：
  DELETE /api/users/organization/{org_id}        删旧组织
  POST   /api/users/organizations  {"name":"1"}  建新组织，返回新 org uuid
  GET    /api/workspaces?page=1&page_size=1000    新组织会自动带一个 default ws
  POST   /api/billing/api-keys  {workspace_uuid}  在新 ws 上发 key

只由 budget_check 巡检在「额度耗尽」时触发，实时 402 不走这条路（402 只换号）。
失败处理保持简单：返回 ok=False + 错误描述，调用方决定是否标记账号不可用。
"""
import json
from dataclasses import dataclass

import httpx

from .billing import CONSOLE, KRATOS_LOGIN, UA, BillingError

_ORG_NAME = "1"  # 与注册脚本保持一致：组织名就叫 "1"


@dataclass(slots=True)
class RebuildResult:
    """一次重建的结果。ok=False 时其余字段为空，error 描述失败原因。"""
    ok: bool = False
    org_id: str = ""
    workspace_id: str = ""
    api_key: str = ""
    key_id: str = ""
    session: str = ""       # 若中途刷新了 console_session 则非空
    error: str = ""


class OrgRebuilder:
    """删旧组织、建新组织、在新 default workspace 上发新 key。

    会话策略与 BudgetClient 一致：优先用注册时存的 console_session，失效或没有
    才用 mistral_password 登一次。登录方法复用 billing 里的流程。
    """

    def __init__(self, timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        self._timeout = timeout
        self._transport = transport

    async def rebuild(self, acc) -> RebuildResult:
        """对一个已耗尽额度的账号执行重建。acc 是 core.pool.Account。"""
        old_org_id = (acc.org_id or "").strip()
        if not old_org_id:
            return RebuildResult(ok=False, error="账号没有 org_id，不知道删哪个组织")
        if not (acc.console_session or acc.mistral_password):
            return RebuildResult(ok=False, error="既没会话也没密码，登不进控制台")

        try:
            cookies, refreshed_session = await self._ensure_session(acc)
        except BillingError as e:
            return RebuildResult(ok=False, error=f"登录失败：{e}")

        try:
            csrf = self._csrf_from(cookies)
            await self._delete_org(cookies, csrf, old_org_id)
            new_org = await self._create_org(cookies, csrf)
            new_ws = await self._default_workspace(cookies, csrf)
            new_key, new_key_id = await self._create_key(cookies, csrf, new_ws)
        except BillingError as e:
            return RebuildResult(ok=False, error=str(e))

        return RebuildResult(
            ok=True,
            org_id=new_org,
            workspace_id=new_ws,
            api_key=new_key,
            key_id=new_key_id,
            session=refreshed_session or "",
        )

    # ---------- 会话 ----------

    async def _ensure_session(self, acc) -> tuple[httpx.Cookies, str]:
        """返回可用的 cookie jar；如果中途用密码登过，把新会话 JSON 也带回去。

        存储的 console_session 只含 ory_session 身份 cookie，csrftoken 是控制台
        首次访问时后端 set 的，需要单独补一次。登过的 jar 自带 csrftoken，不用补。
        """
        cookies = httpx.Cookies()
        if acc.console_session:
            try:
                stored = json.loads(acc.console_session)
                if isinstance(stored, dict):
                    for name, value in stored.items():
                        cookies.set(name, value, domain="admin.mistral.ai")
            except (json.JSONDecodeError, TypeError):
                pass
            if cookies:
                await self._ensure_csrf(cookies)
                return cookies, ""
        # 没会话或解析失败，登一次
        jar = await self._login(acc.email, acc.mistral_password)
        return jar, json.dumps(self._session_cookies(jar))

    async def _ensure_csrf(self, cookies) -> None:
        """jar 里没 csrftoken 就访问控制台首页让后端 set 一个。

        用一个共享 jar 的 client，response 的 set-cookie 会写进 client 自身的
        cookies；再把 client 拿到的 cookie 合并回传入的 jar。直接传 jar 给
        httpx 的话，set-cookie 不一定会写回外部 jar（httpx 版本相关）。
        """
        if self._csrf_from(cookies):
            return
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                     cookies=cookies, follow_redirects=True) as c:
            await c.get(CONSOLE + "/organization",
                        headers={"User-Agent": UA, "Accept": "text/html"})
            # client 关闭前把它新拿到的 cookie 合并回传入 jar
            for cookie in c.cookies.jar:
                if not cookies.get(cookie.name, domain=cookie.domain):
                    cookies.set(cookie.name, cookie.value, domain=cookie.domain,
                                path=cookie.path or "/")

    async def _login(self, email: str, password: str) -> httpx.Cookies:
        """与 BudgetClient._login 同流程：拿 Kratos 流程 → POST 密码 → 返回 cookie jar。"""
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True,
                                     transport=self._transport,
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

    @staticmethod
    def _session_cookies(jar) -> dict:
        """只留身份 cookie，与 billing.session_cookies 一致。"""
        return {c.name: c.value for c in getattr(jar, "jar", jar)
                if c.name.startswith("ory_session")}

    @staticmethod
    def _csrf_from(jar) -> str:
        return next((c.value for c in getattr(jar, "jar", jar)
                     if c.name == "csrftoken"), "")

    # ---------- 控制台调用 ----------

    def _headers(self, csrf: str) -> dict:
        return {"User-Agent": UA, "Accept": "*/*",
                "Origin": CONSOLE, "Referer": CONSOLE + "/organization",
                "X-Csrftoken": csrf}

    async def _delete_org(self, cookies, csrf, org_id) -> None:
        url = f"{CONSOLE}/api/users/organization/{org_id}"
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                     cookies=cookies, follow_redirects=True) as c:
            r = await c.delete(url, headers=self._headers(csrf))
        if r.status_code != 200:
            raise BillingError(f"删组织失败 {r.status_code}：{r.text[:200]}")

    async def _create_org(self, cookies, csrf) -> str:
        url = f"{CONSOLE}/api/users/organizations"
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                     cookies=cookies, follow_redirects=True) as c:
            r = await c.post(url, headers=self._headers(csrf), json={"name": _ORG_NAME})
        if r.status_code != 200:
            raise BillingError(f"建组织失败 {r.status_code}：{r.text[:200]}")
        org_id = (r.json() or {}).get("uuid")
        if not org_id:
            raise BillingError("建组织响应里没有 uuid")
        return org_id

    async def _default_workspace(self, cookies, csrf) -> str:
        url = f"{CONSOLE}/api/workspaces?page=1&page_size=1000"
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                     cookies=cookies, follow_redirects=True) as c:
            r = await c.get(url, headers=self._headers(csrf))
        if r.status_code != 200:
            raise BillingError(f"列 workspace 失败 {r.status_code}")
        items = (r.json() or {}).get("items") or []
        ws = next((w["uuid"] for w in items if w.get("is_default")), None)
        if not ws:
            raise BillingError("新组织里没有 default workspace")
        return ws

    async def _create_key(self, cookies, csrf, workspace_uuid) -> tuple[str, str]:
        url = f"{CONSOLE}/api/billing/api-keys"
        body = {"name": "auto", "workspace_uuid": workspace_uuid,
                "primitive_access_scope": "shared_only"}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                     cookies=cookies, follow_redirects=True) as c:
            r = await c.post(url, headers=self._headers(csrf), json=body)
        if r.status_code != 200:
            raise BillingError(f"发 key 失败 {r.status_code}：{r.text[:200]}")
        data = r.json() or {}
        key = data.get("key")
        if not key:
            raise BillingError("发 key 响应里没有 key")
        return key, data.get("key_id") or ""
