"""Selected market-only methods from pinned cfquant; see UPSTREAM.md."""
import inspect

L2_PERIODS = frozenset()


class QmtMethods(object):
    def _get_market_data_ex(self, params):
        func = self._get_callable("get_market_data_ex")
        if not func:
            raise NotImplementedError("get_market_data_ex not found")
        return func(
            params.get("field_list", []),
            params.get("stock_list", []),
            params.get("period", "1d"),
            params.get("start_time", ""),
            params.get("end_time", ""),
            params.get("count", -1),
            params.get("dividend_type", "none"),
            False if params.get("period") in L2_PERIODS else params.get("fill_data", True),
        )

    def _get_local_data(self, params):
        modern = self._get_callable("get_market_data_ex")
        if modern:
            # The ninth QMT argument disables subscription and reads local data.
            arguments = (
                params.get("field_list", []),
                params.get("stock_list") or params.get("code_list") or ([params["stock_code"]] if params.get("stock_code") else []),
                params.get("period", "1d"),
                params.get("start_time", ""),
                params.get("end_time", ""),
                params.get("count", -1),
                params.get("dividend_type", "none"),
                params.get("fill_data", True),
                False,
            )
            try:
                signature = inspect.signature(modern)
            except (TypeError, ValueError):
                signature = None
            if signature is not None:
                try:
                    signature.bind(*arguments)
                except TypeError:
                    modern = None
            if modern is not None:
                return modern(*arguments)
        func = self._get_callable("get_local_data")
        if not func:
            raise NotImplementedError("get_local_data requires local QMT data access")
        stock_code = self._first_param(params, ("stock_code", "stockcode", "stock", "code"), "")
        stock_list = self._list_param(params.get("stock_list", params.get("code_list", [])))
        if not stock_code and stock_list:
            return dict((code, self._call_local_data(func, code, params)) for code in stock_list)
        return self._call_local_data(func, stock_code, params)

    def _call_local_data(self, func, stock_code, params):
        start_time = params.get("start_time") or params.get("start_date") or "19700101"
        end_time = params.get("end_time") or params.get("end_date") or "22010101"
        return self._call_variants(func, [
            ((
                stock_code,
                start_time,
                end_time,
                params.get("period", "follow"),
                params.get("divid_type", params.get("dividend_type", "none")),
                params.get("count", -1),
            ), {}),
            ((
                stock_code,
                start_time,
                end_time,
                params.get("period", "follow"),
                params.get("divid_type", params.get("dividend_type", "none")),
            ), {}),
            ((
                stock_code,
                start_time,
                end_time,
            ), {}),
            ((stock_code,), {}),
        ])

    def _get_instrument_detail(self, params):
        params = params or {}
        stock_code = self._first_param(params, ("stock_code", "stockcode", "stock", "code"), "")
        iscomplete = params.get("iscomplete", params.get("is_complete", params.get("complete", False)))
        for name in ("get_instrument_detail", "get_instrumentdetail"):
            func = self._get_callable(name)
            if not func:
                continue
            try:
                if name == "get_instrumentdetail":
                    result = func(stock_code)
                    if isinstance(result, dict):
                        result = dict(result)
                        result["cfquant_detail_partial"] = True
                        result["cfquant_detail_source"] = name
                    return result
                return self._call_variants(func, [((stock_code, iscomplete), {}), ((stock_code,), {})])
            except Exception as e:
                if not self._instrument_detail_callable_missing(e):
                    raise
                self._log("%s unavailable: %s" % (name, e))
        self._log("get_instrument_detail not found, using fallback")
        return self._fallback_instrument_detail(params, stock_code)

    def _require_qmt_callable(self, *names):
        func = self._get_callable(*names)
        if not func:
            raise NotImplementedError("requires QMT callable: %s" % ", ".join(names))
        return func

    def _get_stock_list_in_sector(self, params):
        func = self._require_qmt_callable("get_stock_list_in_sector")
        sector = params.get("sector_name", "")
        timetag = params.get("real_timetag", -1)
        if timetag == -1:
            return func(sector)
        # Never retry without a requested historical timestamp.
        return func(sector, timetag)

    def _get_sector_list(self):
        return list(dict.fromkeys(item['name'] for item in self._get_sector_tree()))

    def _get_sector_tree(self):
        func = self._require_qmt_callable("get_sector_list")
        pending, visited, sectors = [("", [])], set(), []
        while pending:
            node, ancestors = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            if len(visited) > 10000:
                raise ValueError("QMT sector tree exceeds 10000 nodes")
            info = func(node)
            if not isinstance(info, (list, tuple)) or len(info) != 2:
                raise ValueError("QMT get_sector_list must return [sectors, folders]")
            if any(not isinstance(items, (list, tuple)) for items in info):
                raise ValueError("invalid QMT sector tree node")
            if any(not isinstance(name, str) or not name for items in info for name in items):
                raise ValueError("invalid QMT sector or folder name")
            for sector in info[0]:
                sectors.append({'name': sector, 'path': ancestors})
            pending.extend((folder, ancestors + [folder]) for folder in reversed(info[1]))
        return sectors

    def _first_param(self, params, names, default=None):
        for name in names:
            value = params.get(name)
            if value is not None and value != "":
                return value
        return default

    def _list_param(self, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return [value]

    def _call_variants(self, func, variants):
        last_error = None
        for args, kwargs in variants:
            try:
                return func(*args, **kwargs)
            except TypeError as e:
                last_error = e
                continue
        if last_error:
            raise last_error
        return func()

    def _get_callable(self, *names):
        owners = [self.globals_dict]
        if self.context is not None:
            owners.append(self.context)
            inner_context = getattr(self.context, "context", None)
            if inner_context is not None and inner_context is not self.context:
                owners.append(inner_context)
        for owner in owners:
            for name in names:
                if isinstance(owner, dict):
                    func = owner.get(name)
                else:
                    func = getattr(owner, name, None)
                if callable(func):
                    return func
        return None
