"""4tochki (b2b.4tochki.ru, «Форточки») SOAP API: the access check and the catalog. Reads only (Ping, GetWarehouses, GetFindTyre,
GetGoodsInfo); nothing is created or changed there. Help: docs/integrations/4tochki-api.md. The login and password go in every call,
so the only address they may be sent to is the supplier's own, and no error text carries them.
The request shapes follow the service's own schema (its WSDL, 2026-10-21): the search filter has its own namespace, lists are WCF
arrays, and the members of the filter go in the schema's order (the service reads them in that order)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape

from . import reliability
from .transport import Transport, urllib_transport

ENDPOINT = "https://api-b2b.4tochki.ru/WCF/ClientService.svc"
NAMESPACE = "Wcf.ClientService.Client.WebAPI.TS3"
WANTED_WAREHOUSE = "Санкт-Петербург 2"
SEARCH_NS = "http://schemas.datacontract.org/2004/07/TS3.Domain.Models.Client.ClientSoapService.SearchTires"
ARRAYS_NS = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
PAGE_SIZE = 2000            # the most the search gives on one page
CARDS_PER_CALL = 200        # the most codes GetGoodsInfo takes at once
CARD_FIELDS = ("width", "height", "diameter", "load_index", "speed_index", "puncture", "strengthening", "omolog", "name", "brand", "model")


class TochkiError(Exception):
    pass


def _fault(status: int, raw: bytes) -> bool:
    """A SOAP Fault comes as 500: the server answered (a wrong password, a wrong argument), it is not down."""
    return b":Fault>" in raw or b"<Fault>" in raw


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _fields(node: ET.Element) -> Dict[str, str]:
    """The simple members of one record: name -> text (a nil member is empty)."""
    return {_local(child.tag): (child.text or "").strip() for child in node if len(child) == 0}


def _child(node: ET.Element, name: str) -> Optional[ET.Element]:
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _records(root: ET.Element, name: str) -> List[ET.Element]:
    return [n for n in root.iter() if _local(n.tag) == name]


def warehouse_rows(root: ET.Element) -> List[dict]:
    return [{"id": int(f["id"]) if f.get("id", "").isdigit() else None, "name": f.get("name", ""), "short": f.get("shortName", ""),
             "delivery": f.get("haveDelivery", "").lower() == "true"} for f in map(_fields, _records(root, "WarehouseInfo"))]


def pick_warehouse(rows: List[dict], wanted: str = WANTED_WAREHOUSE) -> int:
    """The id of the warehouse named so («Санкт-Петербург 2», not «Санкт-Петербург 21»); several with that name: the one with delivery."""
    want = " ".join(wanted.split()).casefold()

    def named(row):
        for text in (row["name"], row["short"]):
            text = " ".join(text.split()).casefold()
            if text == want or (text.startswith(want) and not text[len(want)].isdigit()):
                return True
        return False
    found = [r for r in rows if r["id"] is not None and named(r)]
    if len(found) > 1:
        found = [r for r in found if r["delivery"]] or found
    if len(found) != 1:
        raise TochkiError("Склад «%s» %s." % (wanted, "не найден в списке складов" if not found else "найден несколько раз"))
    return found[0]["id"]


def find_tyre_xml(warehouse_id: int, page: int, page_size: int = PAGE_SIZE) -> str:
    """Passenger tires of one warehouse, one page. type_list goes before wrh_list: that is their order in the schema."""
    return ('<filter xmlns:f="%(f)s" xmlns:a="%(a)s"><f:type_list><a:string>car</a:string></f:type_list>'
            '<f:wrh_list><a:int>%(w)d</a:int></f:wrh_list></filter><page>%(p)d</page><pageSize>%(s)d</pageSize>') % {
        "f": SEARCH_NS, "a": ARRAYS_NS, "w": int(warehouse_id), "p": int(page), "s": int(page_size)}


def goods_info_xml(codes: Iterable[str]) -> str:
    return '<code_list xmlns:a="%s">%s</code_list>' % (ARRAYS_NS, "".join("<a:string>%s</a:string>" % escape(c) for c in codes))


class TochkiClient:
    def __init__(self, login: str, password: str, http: Transport = urllib_transport):
        self._login = login
        self._password = password
        self._http = http

    def __repr__(self) -> str:
        return "TochkiClient(login=%s)" % ("set" if self._login else "empty")

    def _post(self, method: str, extra_xml: str = "") -> Tuple[int, bytes]:
        if not self._login or not self._password:
            raise TochkiError("Логин или пароль API не заданы.")
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            '<%(m)s xmlns="%(ns)s"><login>%(login)s</login><password>%(password)s</password>%(extra)s</%(m)s>'
            '</s:Body></s:Envelope>'
        ) % {"m": method, "ns": NAMESPACE, "login": escape(self._login), "password": escape(self._password), "extra": extra_xml}
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"%s/ClientService/%s"' % (NAMESPACE, method)}
        status, _, raw = reliability.call(lambda: self._http("POST", ENDPOINT, headers, body.encode("utf-8")), safe=True,   # every method only reads
                                          policy=reliability.TOCHKI, service="tochki", answered=_fault)
        return status, raw

    def _call(self, method: str, extra_xml: str = "") -> ET.Element:
        status, raw = self._post(method, extra_xml)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            root = None
        if root is None:
            raise TochkiError("%s: ответ не разобран (код %s)." % (method, status))
        for node in root.iter():
            if _local(node.tag) == "Fault":
                raise TochkiError("%s: сервер вернул ошибку (код %s)." % (method, status))
        if status != 200:
            raise TochkiError("%s: код %s." % (method, status))
        for result in root.iter():
            if _local(result.tag).endswith("Result"):
                error = _child(result, "error")
                if error is not None and len(error):
                    detail = _fields(error)
                    if detail.get("code", "0") not in ("", "0"):
                        raise TochkiError("%s: ошибка поставщика %s: %s" % (method, detail["code"], self._scrub(detail.get("comment", ""))[:200]))
                break
        return root

    def _scrub(self, text: str) -> str:
        for secret in (self._password, self._login):                    # a server may repeat what it was sent in its error text
            if secret:
                text = text.replace(secret, "***")
        return text

    def ping(self) -> bool:
        for node in self._call("Ping").iter():
            if _local(node.tag) == "PingResult":
                return (node.text or "").strip().lower() == "true"
        raise TochkiError("Ping: в ответе нет результата.")

    def warehouses(self) -> List[dict]:
        return warehouse_rows(self._call("GetWarehouses"))

    def warehouse_id(self, wanted: str = WANTED_WAREHOUSE) -> int:
        return pick_warehouse(self.warehouses(), wanted)

    def find_tyre_page(self, warehouse_id: int, page: int, page_size: Optional[int] = None) -> Tuple[List[dict], int]:
        """One page of the search: the positions (their prices and stock per warehouse under `whpr`) and the number of pages."""
        root = self._call("GetFindTyre", find_tyre_xml(warehouse_id, page, page_size or PAGE_SIZE))
        pages = [n for n in root.iter() if _local(n.tag) == "totalPages"]
        if not pages or not (pages[0].text or "").strip().isdigit():
            raise TochkiError("GetFindTyre: в ответе нет числа страниц.")
        items = []
        for node in _records(root, "TyrePriceRest"):
            row = _fields(node)
            whpr = _child(node, "whpr")
            row["whpr"] = [_fields(w) for w in whpr if _local(w.tag) == "wh_price_rest"] if whpr is not None else []
            items.append(row)
        return items, int(pages[0].text.strip())

    def goods_info(self, codes: List[str]) -> Dict[str, dict]:
        """Tire cards by code (at most 200 a call): size, load and speed index, RunFlat, XL, homologation."""
        if len(codes) > CARDS_PER_CALL:
            raise ValueError("не больше %d кодов за вызов" % CARDS_PER_CALL)
        cards = {}
        for node in _records(self._call("GetGoodsInfo", goods_info_xml(codes)), "TyreContainer"):
            row = _fields(node)
            if row.get("code"):
                cards[row["code"]] = {k: row.get(k, "") for k in CARD_FIELDS}
        return cards

    def check(self) -> str:
        if not self.ping():
            raise TochkiError("Логин или пароль не приняты.")
        try:                                       # the same exact name as the catalog load uses («…2», not «…21»)
            found = "виден (№%d)" % pick_warehouse(self.warehouses())
        except TochkiError:
            found = "в списке не найден"
        return "доступ есть; склад «%s» %s" % (WANTED_WAREHOUSE, found)

    def sample(self) -> str:
        """What the supplier really answers, for writing the catalog loader: the warehouse list, one small search page and the cards of its first codes.
        The answers are kept as they came (they hold no login or password); a failed call keeps its answer too, the server names what it expected there."""
        parts = []
        status, raw = self._post("GetWarehouses")
        parts.append("<!-- GetWarehouses: HTTP %s -->\n%s" % (status, raw.decode("utf-8", "replace")))
        try:
            warehouse = pick_warehouse(warehouse_rows(ET.fromstring(raw)))
        except (ET.ParseError, TochkiError):
            warehouse = None
        codes: List[str] = []
        if warehouse is not None:
            status, raw = self._post("GetFindTyre", find_tyre_xml(warehouse, 0, 3))
            parts.append("<!-- GetFindTyre: HTTP %s -->\n%s" % (status, raw.decode("utf-8", "replace")))
            try:
                codes = [(_fields(n).get("code") or "") for n in _records(ET.fromstring(raw), "TyrePriceRest")]
            except ET.ParseError:
                codes = []
        codes = [c for c in codes if c][:3]
        if codes:
            status, raw = self._post("GetGoodsInfo", goods_info_xml(codes))
            parts.append("<!-- GetGoodsInfo: HTTP %s -->\n%s" % (status, raw.decode("utf-8", "replace")))
        return self._scrub("\n\n".join(parts))
