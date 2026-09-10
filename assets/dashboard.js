(function () {
    "use strict";

    if (window.__dashboardControllerActive) return;
    window.__dashboardControllerActive = true;

    var knowledgeSummaryActive = false;
    var latestKnowledgeSummary = null;
    var comparisonMode = "data";
    var detailComparisonMode = "data";

    function queryDrawer() {
        return document.querySelector(".query-drawer");
    }

    function setDrawer(open) {
        var drawer = queryDrawer();
        if (!drawer) return;
        drawer.open = Boolean(open);
        document.body.classList.toggle("query-drawer-open", Boolean(open));
    }

    function ensureDrawerChrome() {
        var shell = document.querySelector(".app-shell") || document.body;
        var backdrop = document.querySelector(".dashboard-drawer-backdrop");
        if (!backdrop) {
            backdrop = document.createElement("div");
            backdrop.className = "dashboard-drawer-backdrop";
            backdrop.addEventListener("click", function () { setDrawer(false); });
            shell.appendChild(backdrop);
        }
        if (shell !== document.body && backdrop.parentElement !== shell) shell.appendChild(backdrop);

        var close = document.querySelector(".dashboard-drawer-close");
        if (!close) {
            close = document.createElement("button");
            close.type = "button";
            close.className = "dashboard-drawer-close";
            close.setAttribute("aria-label", "关闭查询条件");
            close.textContent = "×";
            close.addEventListener("click", function () { setDrawer(false); });
            shell.appendChild(close);
        }
        if (shell !== document.body && close.parentElement !== shell) shell.appendChild(close);

        var drawer = queryDrawer();
        if (drawer && !drawer.dataset.dashboardBound) {
            drawer.dataset.dashboardBound = "1";
            drawer.addEventListener("toggle", function () {
                document.body.classList.toggle("query-drawer-open", drawer.open);
            });
        }
    }

    function cellText(row, columnId, fallbackIndex) {
        var cell = row && row.querySelector('[data-dash-column="' + columnId + '"]');
        if (!cell && row) cell = row.querySelectorAll("td")[fallbackIndex];
        return cell ? cell.textContent.trim() : "";
    }

    function formatSimilarity(value) {
        var numeric = parseFloat(String(value === null || value === undefined ? "" : value).replace("%", ""));
        if (!Number.isFinite(numeric)) return "—";
        if (numeric <= 1) numeric *= 100;
        return Math.max(0, Math.min(100, numeric)).toFixed(1) + "%";
    }

    function formatCarNo(value) {
        var text = value === null || value === undefined ? "" : String(value).trim();
        return /^\d$/.test(text) ? text.padStart(2, "0") : (text || "—");
    }

    function updateTopWorkOrder() {
        if (knowledgeSummaryActive) return;
        var table = document.getElementById("top_table");
        var rows = table ? Array.prototype.slice.call(table.querySelectorAll("tbody tr")) : [];
        var row = rows.find(function (candidate) {
            var value = cellText(candidate, "工单号", 1);
            return candidate.querySelectorAll("td").length && value && value !== "工单号";
        });
        var workOrder = cellText(row, "工单号", 1) || "—";
        var train = cellText(row, "车号", 2) || "—";
        var similarity = cellText(row, "相似度", 3) || "—";
        var similarityWarning = cellText(row, "匹配说明", 4);
        var workOrderTarget = document.querySelector(".dashboard-top-work-order");
        var trainTarget = document.querySelector(".dashboard-top-train");
        var similarityTarget = document.querySelector(".dashboard-top-similarity");
        if (workOrderTarget && workOrderTarget.textContent !== workOrder) workOrderTarget.textContent = workOrder;
        if (trainTarget && trainTarget.textContent !== "车号 " + train) trainTarget.textContent = "车号 " + train;
        var similarityText = formatSimilarity(similarity);
        if (similarityTarget && similarityTarget.textContent !== similarityText) similarityTarget.textContent = similarityText;
        setSimilarityWarning(similarityWarning);

        var numeric = parseFloat(String(similarity).replace("%", ""));
        if (numeric <= 1) numeric *= 100;
        var count = Number.isFinite(numeric) ? Math.round(Math.max(0, Math.min(100, numeric)) / 100 * 12) : 0;
        document.querySelectorAll(".similarity-bars i").forEach(function (bar, index) {
            bar.classList.toggle("is-filled", index < count);
        });
    }

    function setText(selector, value) {
        var target = document.querySelector(selector);
        var text = value === null || value === undefined || String(value).trim() === "" ? "—" : String(value);
        if (target && target.textContent !== text) target.textContent = text;
    }

    function setSimilarityWarning(value) {
        var target = document.querySelector(".dashboard-similarity-warning");
        if (!target) return;
        var text = value === null || value === undefined ? "" : String(value).trim();
        if (text && target.dataset.warningText !== text) {
            var label = document.createElement("span");
            var detail = document.createElement("span");
            label.className = "dashboard-similarity-warning-label";
            label.textContent = "⚠ 根因待确认";
            detail.className = "dashboard-similarity-warning-detail";
            detail.textContent = text;
            target.replaceChildren(label, detail);
            target.dataset.warningText = text;
            target.classList.remove("is-expanded");
            target.setAttribute("aria-expanded", "false");
        } else if (!text) {
            target.replaceChildren();
            target.dataset.warningText = "";
            target.classList.remove("is-expanded");
            target.setAttribute("aria-expanded", "false");
        }
        target.title = text ? "点击查看匹配说明" : "";
        target.setAttribute("aria-label", text ? "根因待确认，点击查看匹配说明" : "");
        target.setAttribute("role", "button");
        target.tabIndex = text ? 0 : -1;
        target.style.display = text ? "inline-flex" : "none";
        var summary = target.closest(".work-order-summary");
        if (summary) summary.classList.toggle("has-similarity-warning", Boolean(text));
        if (!target.dataset.clickBound) {
            var toggleWarning = function () {
                if (!target.dataset.warningText) return;
                var expanded = target.classList.toggle("is-expanded");
                target.setAttribute("aria-expanded", String(expanded));
            };
            target.addEventListener("click", function (event) {
                event.stopPropagation();
                toggleWarning();
            });
            target.addEventListener("keydown", function (event) {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    event.stopPropagation();
                    toggleWarning();
                } else if (event.key === "Escape") {
                    target.classList.remove("is-expanded");
                    target.setAttribute("aria-expanded", "false");
                }
            });
            document.addEventListener("click", function (event) {
                if (!target.contains(event.target)) {
                    target.classList.remove("is-expanded");
                    target.setAttribute("aria-expanded", "false");
                }
            });
            target.dataset.clickBound = "true";
        }
    }

    function setDashStore(id, data) {
        if (window.dash_clientside && typeof window.dash_clientside.set_props === "function") {
            window.dash_clientside.set_props(id, {data: data});
        }
    }

    function signalCell(signal, side, detailed) {
        var cell = document.createElement("div");
        cell.className = "signal-side-cell signal-side-" + side + (signal ? "" : " is-empty");
        var name = document.createElement("span");
        var values = document.createElement("strong");
        if (signal) {
            name.textContent = signal.description || "未命名信号";
            if (String(signal.previous || "") === "ramping_finish_status") {
                values.textContent = (detailed ? "VCB 动作时状态：" : "状态 = ") + String(signal.current ?? "∅");
                cell.classList.add("is-status-feature");
            } else {
                values.textContent = String(signal.previous ?? "∅") + " → " + String(signal.current ?? "∅");
            }
        } else {
            name.textContent = "未发生对应变化";
            values.textContent = "—";
        }
        cell.append(name, values);
        return cell;
    }

    function renderSignalRows(list, payload) {
        if (!list) return;
        list.replaceChildren();
        var rows = payload && Array.isArray(payload.signalRows) ? payload.signalRows : [];
        if (!rows.length) {
            list.textContent = "当前查询故障和所选历史工单均无可展示的信号特征。";
            return;
        }
        var detailed = list.classList.contains("signal-change-detail-list");
        if (detailed) {
            var header = document.createElement("div");
            header.className = "signal-compare-header";
            header.innerHTML = "<span>当前故障</span><span>匹配</span><span>历史工单</span>";
            list.appendChild(header);
        }
        rows.forEach(function (signalRow) {
            var row = document.createElement("div");
            var state = signalRow.state === "match" ? "match" : (signalRow.state === "variant" ? "variant" : "different");
            row.className = "signal-compare-row is-" + state;
            var relation = document.createElement("i");
            relation.className = "signal-compare-relation";
            var bothStatus = signalRow.left && signalRow.right &&
                String(signalRow.left.previous || "") === "ramping_finish_status" &&
                String(signalRow.right.previous || "") === "ramping_finish_status";
            relation.textContent = state === "match" ? (detailed ? (bothStatus ? "状态一致" : "变化一致") : "一致") : (state === "variant" ? "近似" : "·");
            row.append(signalCell(signalRow.left, "query", detailed), relation, signalCell(signalRow.right, "work-order", detailed));
            list.appendChild(row);
        });
    }

    function renderDetailSummary(payload) {
        var target = document.querySelector(".signal-comparison-detail-summary");
        if (!target) return;
        target.replaceChildren();
        var items = [
            ["对比工单", payload && payload.workOrder ? payload.workOrder : "—"],
            ["后台相似度", formatSimilarity(payload && payload.similarity)],
            ["当前特征", payload && payload.newSignalCount !== undefined ? payload.newSignalCount : "—"],
            ["匹配特征", payload && payload.matchedSignalCount !== undefined ? payload.matchedSignalCount : "—"]
        ];
        if (payload && payload.similarityWarning) items.push(["匹配说明", payload.similarityWarning]);
        items.forEach(function (item) {
            var block = document.createElement("div");
            var label = document.createElement("span");
            var value = document.createElement("strong");
            label.textContent = item[0];
            value.textContent = item[1];
            if (item[0] === "匹配说明") block.classList.add("is-wide");
            block.append(label, value);
            target.appendChild(block);
        });
    }

    function applyKnowledgeSummary(payload) {
        if (!payload) return;
        resetRawChangeAlignment(false);
        latestKnowledgeSummary = payload;
        knowledgeSummaryActive = true;
        setText(".dashboard-top-work-order", payload.workOrder);
        setText(".dashboard-top-train", "车号 " + (payload.train || "—"));
        setText(".dashboard-top-car", "车厢 " + formatCarNo(payload.carNo));
        setText(".dashboard-top-similarity", formatSimilarity(payload.similarity));
        setSimilarityWarning(payload.similarityWarning);
        setText(".structured-fault", payload.fault);
        setText(".structured-component", payload.component);
        setText(".structured-cause", payload.cause);
        setText(".structured-action", payload.action);
        setText(".structured-result", payload.result);
        setText(".signal-new-count", payload.newSignalCount);
        setText(".signal-history-count", payload.historySignalCount);
        setText(".signal-match-count", payload.matchedSignalCount);
        renderSignalRows(document.querySelector(".signal-change-list:not(.signal-change-detail-list)"), payload);
        renderSignalRows(document.querySelector(".signal-change-detail-list"), payload);
        renderDetailSummary(payload);
        setDashStore("dashboard_selected_work_order", payload.workOrder || null);
    }

    function detailRawPlot() {
        return document.querySelector("#signal_raw_comparison_detail .js-plotly-plot");
    }

    function refreshRawAlignmentButton() {
        var button = document.getElementById("align_raw_change_points");
        var plot = detailRawPlot();
        if (!button) return;
        if (!plot || !Array.isArray(plot.data) || !plot.data.length) {
            button.disabled = true;
            return;
        }
        if (plot.__rawAlignmentDataRef !== plot.data) {
            plot.__rawAlignmentDataRef = plot.data;
            plot.__rawAlignmentOriginalX = plot.data.map(function (trace) {
                return Array.from(trace.x || []);
            });
            button.dataset.aligned = "false";
            button.textContent = "变化点对齐";
            var freshStatus = document.getElementById("signal_raw_comparison_detail_status");
            button.dataset.baseStatus = freshStatus ? freshStatus.textContent.trim() : "";
        }
        var canAlign = plot.data.some(function (trace) {
            var meta = trace.meta || {};
            var shift = Number(meta.alignment_shift_seconds || 0);
            return meta.comparison_role === "history" && Number.isFinite(shift) && Math.abs(shift) > 0.0005;
        });
        button.disabled = !canAlign;
        if (!canAlign) button.title = "当前两组曲线的变化点已经对齐";
    }

    function setRawChangeAlignment(aligned, animate) {
        var button = document.getElementById("align_raw_change_points");
        var plot = detailRawPlot();
        if (!button || !plot || !Array.isArray(plot.data) || !plot.data.length) return;
        refreshRawAlignmentButton();
        if (!Array.isArray(plot.__rawAlignmentOriginalX)) return;

        var traceIndices = [];
        var updates = [];
        var appliedShifts = [];
        plot.data.forEach(function (trace, index) {
            var meta = trace.meta || {};
            if (meta.comparison_role !== "history") return;
            var shift = Number(meta.alignment_shift_seconds || 0);
            var original = plot.__rawAlignmentOriginalX[index] || [];
            traceIndices.push(index);
            updates.push({
                x: aligned ? original.map(function (value) { return Number(value) + shift; }) : Array.from(original)
            });
            if (Number.isFinite(shift)) appliedShifts.push(shift);
        });
        if (!traceIndices.length || !window.Plotly) return;

        var duration = animate ? 720 : 0;
        window.Plotly.animate(
            plot,
            {data: updates, traces: traceIndices},
            {
                transition: {duration: duration, easing: "cubic-in-out"},
                frame: {duration: duration, redraw: false},
                mode: "immediate"
            }
        );
        button.dataset.aligned = aligned ? "true" : "false";
        button.textContent = aligned ? "恢复 T0 对齐" : "变化点对齐";
        var status = document.getElementById("signal_raw_comparison_detail_status");
        var baseStatus = button.dataset.baseStatus || (status ? status.textContent.trim() : "");
        if (status) {
            if (aligned) {
                var maxShift = appliedShifts.length ? Math.max.apply(null, appliedShifts.map(Math.abs)) : 0;
                status.textContent = baseStatus + " 历史工单整组曲线已按变化点整体对齐（统一平移 " + maxShift.toFixed(2) + " 秒，仅改变展示位置）。";
            } else {
                status.textContent = baseStatus;
            }
        }
    }

    function resetRawChangeAlignment(animate) {
        var button = document.getElementById("align_raw_change_points");
        if (button && button.dataset.aligned === "true") setRawChangeAlignment(false, Boolean(animate));
    }

    function setMainComparisonMode(mode) {
        comparisonMode = mode === "data" ? "data" : "signal";
        var signalView = document.getElementById("signal_comparison_signal_view");
        var dataView = document.getElementById("signal_comparison_data_view");
        if (signalView) signalView.style.display = comparisonMode === "signal" ? "" : "none";
        if (dataView) dataView.style.display = comparisonMode === "data" ? "" : "none";
        document.querySelectorAll(".comparison-mode-switch:not(.comparison-mode-switch-detail) .comparison-mode-button").forEach(function (button) {
            button.classList.toggle("is-active", button.dataset.comparisonMode === comparisonMode);
        });
        setDashStore("signal_comparison_mode", comparisonMode);
        window.setTimeout(function () { window.dispatchEvent(new Event("resize")); }, 40);
    }

    function setDetailComparisonMode(mode) {
        detailComparisonMode = mode === "data" ? "data" : "signal";
        if (detailComparisonMode !== "data") resetRawChangeAlignment(false);
        var signalView = document.getElementById("signal_comparison_detail_signal_view");
        var dataView = document.getElementById("signal_comparison_detail_data_view");
        if (signalView) signalView.style.display = detailComparisonMode === "signal" ? "" : "none";
        if (dataView) dataView.style.display = detailComparisonMode === "data" ? "" : "none";
        document.querySelectorAll(".comparison-mode-switch-detail .comparison-mode-button").forEach(function (button) {
            button.classList.toggle("is-active", button.dataset.comparisonMode === detailComparisonMode);
        });
        setDashStore("signal_comparison_detail_mode", detailComparisonMode);
        window.setTimeout(function () { window.dispatchEvent(new Event("resize")); }, 40);
    }

    function setSignalComparisonModal(open) {
        var modal = document.getElementById("signal_comparison_dashboard_modal");
        if (!modal) return;
        modal.classList.toggle("is-open", Boolean(open));
        document.body.classList.toggle("signal-comparison-modal-open", Boolean(open));
        setDashStore("signal_comparison_detail_open", Boolean(open));
        if (open) {
            renderSignalRows(document.querySelector(".signal-change-detail-list"), latestKnowledgeSummary);
            renderDetailSummary(latestKnowledgeSummary);
            setDetailComparisonMode(comparisonMode);
            window.setTimeout(refreshRawAlignmentButton, 160);
        } else {
            resetRawChangeAlignment(false);
        }
    }

    function ensureSignalActions() {
        document.querySelectorAll(".comparison-mode-switch:not(.comparison-mode-switch-detail) .comparison-mode-button").forEach(function (button) {
            if (button.dataset.dashboardBound) return;
            button.dataset.dashboardBound = "1";
            button.addEventListener("click", function () { setMainComparisonMode(button.dataset.comparisonMode); });
        });
        document.querySelectorAll(".comparison-mode-switch-detail .comparison-mode-button").forEach(function (button) {
            if (button.dataset.dashboardBound) return;
            button.dataset.dashboardBound = "1";
            button.addEventListener("click", function () { setDetailComparisonMode(button.dataset.comparisonMode); });
        });

        var openButton = document.querySelector(".open-signal-comparison");
        if (openButton && !openButton.dataset.dashboardBound) {
            openButton.dataset.dashboardBound = "1";
            openButton.addEventListener("click", function () { setSignalComparisonModal(true); });
        }
        var closeButton = document.querySelector(".signal-comparison-dashboard-close");
        if (closeButton && !closeButton.dataset.dashboardBound) {
            closeButton.dataset.dashboardBound = "1";
            closeButton.addEventListener("click", function () { setSignalComparisonModal(false); });
        }
        var backdrop = document.querySelector(".signal-comparison-dashboard-backdrop");
        if (backdrop && !backdrop.dataset.dashboardBound) {
            backdrop.dataset.dashboardBound = "1";
            backdrop.addEventListener("click", function () { setSignalComparisonModal(false); });
        }
        var alignButton = document.getElementById("align_raw_change_points");
        if (alignButton && !alignButton.dataset.dashboardBound) {
            alignButton.dataset.dashboardBound = "1";
            alignButton.addEventListener("click", function () {
                setRawChangeAlignment(alignButton.dataset.aligned !== "true", true);
            });
        }
        refreshRawAlignmentButton();
    }

    function boot() {
        ensureDrawerChrome();
        ensureSignalActions();
        updateTopWorkOrder();
        window.addEventListener("message", function (event) {
            if (event.data && event.data.type === "1757-kg-summary") applyKnowledgeSummary(event.data.payload);
        });
        var root = document.getElementById("_dash-app-content") || document.body;
        var observer = new MutationObserver(function () {
            ensureDrawerChrome();
            ensureSignalActions();
            updateTopWorkOrder();
        });
        observer.observe(root, {childList: true, subtree: true, characterData: true});
        document.addEventListener("keydown", function (event) {
            if (event.key !== "Escape") return;
            if (document.body.classList.contains("signal-comparison-modal-open")) setSignalComparisonModal(false);
            else if (document.body.classList.contains("query-drawer-open")) setDrawer(false);
        });
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else boot();
}());
