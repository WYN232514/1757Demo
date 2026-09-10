(function () {
    "use strict";

    /* dashboard.js is also an auto-loaded Dash asset.  Load it explicitly from
       this established asset entry point as a compatibility fallback for
       deployments whose cached Dash index predates the new dashboard asset. */
    if (!window.__dashboardControllerRequested) {
        window.__dashboardControllerRequested = true;
        var dashboardController = document.createElement("script");
        dashboardController.src = new URL("dashboard.js", document.currentScript.src).toString();
        dashboardController.defer = true;
        document.head.appendChild(dashboardController);
    }

    function extractWorkOrder(node) {
        var raw = (node.textContent || "").trim();
        // Plotly 将两行刻度文本拼成 "C1｜5001505712T25/9"；号码后面
        // 紧跟车号字母，因此不能使用 \b 作为右边界。
        var match = raw.match(/\d{10}/);
        if (match) return match[0];
        return raw.indexOf("新故障") >= 0 ? "新故障" : "";
    }

    function publishSelection(workOrder) {
        if (!workOrder || !window.dash_clientside || !window.dash_clientside.set_props) {
            return;
        }
        window.dash_clientside.set_props("work_order_detail_selection", {
            data: {work_order: workOrder, timestamp: Date.now()}
        });
    }

    function bindAxisTicks() {
        var root = document.getElementById("work_order_similarity_heatmap");
        if (!root) return;

        root.querySelectorAll(".xtick, .ytick").forEach(function (tick) {
            var text = tick.querySelector("text") || tick;
            tick.style.pointerEvents = "all";
            text.style.pointerEvents = "all";
            text.style.cursor = "pointer";
            text.style.textDecoration = "underline dotted";

            if (tick.dataset.workOrderClickBound === "1") return;
            tick.dataset.workOrderClickBound = "1";
            tick.addEventListener("click", function (event) {
                event.preventDefault();
                event.stopPropagation();
                publishSelection(extractWorkOrder(text));
            }, true);
        });
    }

    function start() {
        bindAxisTicks();
        var observer = new MutationObserver(function () {
            bindAxisTicks();
        });
        observer.observe(document.body, {childList: true, subtree: true});
        window.addEventListener("resize", bindAxisTicks);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();

(function () {
    "use strict";

    var statusHideTimer = 0;

    function text(node, value) {
        if (node && node.textContent !== value) node.textContent = value;
    }

    function firstWorkOrder() {
        var cell = document.querySelector('#top_table td[data-dash-column="工单号"]');
        if (!cell) {
            cell = document.querySelector('#top_table tbody tr:first-child td:nth-child(2)');
        }
        var match = ((cell && cell.textContent) || "").match(/\d{10}/);
        return match ? match[0] : "—";
    }

    function updateHeaderMeta() {
        var train = document.getElementById("train");
        var faultTime = document.getElementById("fault_time");
        var trainValue = (train && train.value || "").trim() || "—";
        var timeValue = (faultTime && faultTime.value || "").trim() || "—";
        text(document.querySelector(".header-current-work-order"), firstWorkOrder());
        text(document.querySelector(".header-current-train"), trainValue);
        text(document.querySelector(".header-current-time"), timeValue);
        text(document.querySelector(".header-data-time"), timeValue);
    }

    function decorateSimilarityCells() {
        document.querySelectorAll('#top_table td[data-dash-column="相似度"]').forEach(function (cell) {
            var raw = cell.dataset.similarityRaw || (cell.textContent || "").trim();
            var value = Number.parseFloat(raw);
            if (!Number.isFinite(value)) return;
            cell.dataset.similarityRaw = raw;
            var displayRaw = raw;
            if (/^\d+(?:\.\d+)?$/.test(raw)) {
                var parts = raw.split(".");
                displayRaw = (parts[1] || "").length >= 4
                    ? raw
                    : parts[0] + "." + ((parts[1] || "") + "0000").slice(0, 4);
            }
            var percent = Math.max(0, Math.min(100, value <= 1 ? value * 100 : value));
            var valueNode = cell.querySelector(".similarity-cell-value");
            var progress = cell.querySelector(".similarity-progress");
            if (!valueNode || !progress) {
                cell.textContent = "";
                valueNode = document.createElement("span");
                valueNode.className = "similarity-cell-value";
                progress = document.createElement("span");
                progress.className = "similarity-progress";
                cell.append(valueNode, progress);
            }
            if (valueNode.textContent !== displayRaw) valueNode.textContent = displayRaw;
            progress.style.setProperty("--similarity-width", percent + "%");
        });
    }

    function refreshPresentation() {
        decorateSimilarityCells();
        updateHeaderMeta();
        var status = document.getElementById("status");
        if (status) {
            var statusValue = (status.textContent || "").trim();
            if (status.dataset.presentationValue !== statusValue) {
                status.dataset.presentationValue = statusValue;
                status.classList.remove("status-line-hidden");
                window.clearTimeout(statusHideTimer);
                if (/^成功/.test(statusValue)) {
                    statusHideTimer = window.setTimeout(function () {
                        status.classList.add("status-line-hidden");
                    }, 2800);
                }
            }
        }
        ["train", "fault_time"].forEach(function (id) {
            var input = document.getElementById(id);
            if (!input || input.dataset.headerSyncBound === "1") return;
            input.dataset.headerSyncBound = "1";
            input.addEventListener("input", updateHeaderMeta);
            input.addEventListener("change", updateHeaderMeta);
        });
        var run = document.getElementById("run");
        if (run && run.dataset.queryDrawerBound !== "1") {
            run.dataset.queryDrawerBound = "1";
            run.addEventListener("click", function () {
                if (window.dash_clientside && window.dash_clientside.set_props) {
                    window.dash_clientside.set_props("manual_query_trigger", {
                        data: {revision: Date.now()}
                    });
                }
                window.setTimeout(closeQueryDrawer, 120);
            });
        }
    }

    function closeQueryDrawer() {
        var drawer = document.querySelector(".query-drawer");
        if (drawer) drawer.removeAttribute("open");
    }

    function startCommandWallPresentation() {
        refreshPresentation();
        ["train", "fault_time"].forEach(function (id) {
            var input = document.getElementById(id);
            if (!input || input.dataset.headerSyncBound === "1") return;
            input.dataset.headerSyncBound = "1";
            input.addEventListener("input", updateHeaderMeta);
            input.addEventListener("change", updateHeaderMeta);
        });

        var run = document.getElementById("run");
        if (run && run.dataset.queryDrawerBound !== "1") {
            run.dataset.queryDrawerBound = "1";
            run.addEventListener("click", function () {
                if (window.dash_clientside && window.dash_clientside.set_props) {
                    window.dash_clientside.set_props("manual_query_trigger", {
                        data: {revision: Date.now()}
                    });
                }
                window.setTimeout(closeQueryDrawer, 120);
            });
        }

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") closeQueryDrawer();
        });

        var observer = new MutationObserver(refreshPresentation);
        observer.observe(document.body, {childList: true, subtree: true});
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", startCommandWallPresentation);
    } else {
        startCommandWallPresentation();
    }
})();
