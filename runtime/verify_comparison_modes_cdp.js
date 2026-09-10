const targets = await (await fetch("http://127.0.0.1:9223/json/list")).json();
const page = targets.find((target) => target.type === "page" && target.url.includes("127.0.0.1:18050"));
if (!page) throw new Error("comparison page target not found");

const socket = new WebSocket(page.webSocketDebuggerUrl);
let sequence = 0;
const pending = new Map();

socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const {resolve, reject} = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result);
});

function call(method, params = {}) {
    return new Promise((resolve, reject) => {
        const id = ++sequence;
        pending.set(id, {resolve, reject});
        socket.send(JSON.stringify({id, method, params}));
    });
}

await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, {once: true});
    socket.addEventListener("error", reject, {once: true});
});

const expression = `
(async () => {
    const waitFor = async (test, timeout = 30000) => {
        const started = Date.now();
        while (!test()) {
            if (Date.now() - started > timeout) throw new Error("browser verification timeout");
            await new Promise(resolve => setTimeout(resolve, 150));
        }
    };
    await waitFor(() => document.querySelector("#data_mode_button") &&
        document.querySelector(".dashboard-top-work-order")?.textContent.trim() === "5001505712");
    await waitFor(() => {
        const plot = document.querySelector("#signal_raw_comparison_compact .js-plotly-plot");
        return document.querySelector("#signal_comparison_data_view")?.style.display !== "none" &&
            Array.isArray(plot?.data) && plot.data.length > 0;
    });
    const compactPlot = document.querySelector("#signal_raw_comparison_compact .js-plotly-plot");
    const compact = {
        signalHidden: document.querySelector("#signal_comparison_signal_view")?.style.display === "none",
        dataVisible: document.querySelector("#signal_comparison_data_view")?.style.display !== "none",
        activeMode: document.querySelector("#data_mode_button")?.classList.contains("is-active"),
        traces: compactPlot?.data?.length || 0,
        names: (compactPlot?.data || []).map(trace => trace.name),
        status: document.querySelector("#signal_raw_comparison_compact_status")?.textContent.trim()
    };
    document.querySelector("#open_signal_comparison").click();
    await waitFor(() => document.querySelector("#signal_comparison_dashboard_modal")?.classList.contains("is-open"));
    await waitFor(() => {
        const plot = document.querySelector("#signal_raw_comparison_detail .js-plotly-plot");
        return document.querySelector("#signal_comparison_detail_data_view")?.style.display !== "none" &&
            Array.isArray(plot?.data) && plot.data.length > 0 &&
            document.querySelector("#align_raw_change_points")?.disabled === false;
    });
    const detailPlot = document.querySelector("#signal_raw_comparison_detail .js-plotly-plot");
    const historyTrace = detailPlot.data.find(trace => trace.meta?.comparison_role === "history");
    const originalFirstX = Number(historyTrace.x[0]);
    document.querySelector("#align_raw_change_points").click();
    await new Promise(resolve => setTimeout(resolve, 1000));
    const alignedFirstX = Number(historyTrace.x[0]);
    const alignedButtonText = document.querySelector("#align_raw_change_points")?.textContent.trim();
    document.querySelector("#align_raw_change_points").click();
    await new Promise(resolve => setTimeout(resolve, 1000));
    const restoredFirstX = Number(historyTrace.x[0]);
    return {
        compact,
        detail: {
            modalOpen: document.querySelector("#signal_comparison_dashboard_modal")?.classList.contains("is-open"),
            signalHidden: document.querySelector("#signal_comparison_detail_signal_view")?.style.display === "none",
            dataVisible: document.querySelector("#signal_comparison_detail_data_view")?.style.display !== "none",
            activeMode: document.querySelector("#detail_data_mode_button")?.classList.contains("is-active"),
            traces: detailPlot?.data?.length || 0,
            status: document.querySelector("#signal_raw_comparison_detail_status")?.textContent.trim(),
            alignButtonEnabled: document.querySelector("#align_raw_change_points")?.disabled === false,
            alignedButtonText,
            animatedShiftSeconds: alignedFirstX - originalFirstX,
            restoredToT0: Math.abs(restoredFirstX - originalFirstX) < 0.0001
        }
    };
})()`;

const evaluated = await call("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true
});
if (evaluated.exceptionDetails) throw new Error(evaluated.exceptionDetails.text);
process.stdout.write(JSON.stringify(evaluated.result.value, null, 2) + "\n");
socket.close();
