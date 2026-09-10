const targets = await (await fetch("http://127.0.0.1:9223/json/list")).json();
const page = targets.find(target => target.type === "page" && target.url.includes("127.0.0.1:18050"));
if (!page) throw new Error("page target not found");
const socket = new WebSocket(page.webSocketDebuggerUrl);
let sequence = 0;
const pending = new Map();
socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const item = pending.get(message.id);
    pending.delete(message.id);
    message.error ? item.reject(new Error(JSON.stringify(message.error))) : item.resolve(message.result);
});
await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, {once: true});
    socket.addEventListener("error", reject, {once: true});
});
function call(method, params = {}) {
    return new Promise((resolve, reject) => {
        const id = ++sequence;
        pending.set(id, {resolve, reject});
        socket.send(JSON.stringify({id, method, params}));
    });
}
const expression = `
(async () => {
    const waitFor = async (test, timeout = 30000) => {
        const started = Date.now();
        while (!test()) {
            if (Date.now() - started > timeout) throw new Error("verification timeout");
            await new Promise(resolve => setTimeout(resolve, 150));
        }
    };
    await waitFor(() => document.querySelector(".dashboard-top-work-order")?.textContent.trim() === "5001505712");
    window.dash_clientside.set_props("dashboard_selected_work_order", {data: "5001331067"});
    await waitFor(() => document.querySelector("#signal_raw_comparison_compact_status")?.textContent.includes("5001331067"));
    const compactPlot = document.querySelector("#signal_raw_comparison_compact .js-plotly-plot");
    const compactTitles = (compactPlot.layout.annotations || []).map(item => item.text);
    document.querySelector("#open_signal_comparison").click();
    await waitFor(() => document.querySelector("#signal_raw_comparison_detail_status")?.textContent.includes("5001331067"));
    await waitFor(() => document.querySelector("#align_raw_change_points")?.disabled === false);
    const detailPlot = document.querySelector("#signal_raw_comparison_detail .js-plotly-plot");
    const distanceHistory = detailPlot.data.find(trace =>
        trace.meta?.dimension === "dir_distance" && trace.meta?.comparison_role === "history"
    );
    const sharedHistory = detailPlot.data.find(trace =>
        trace.meta?.comparison_role === "history" && Math.abs(Number(trace.meta?.alignment_shift_seconds || 0)) > 0.001
    );
    const historyBefore = detailPlot.data
        .filter(trace => trace.meta?.comparison_role === "history")
        .map(trace => Number(trace.x[0]));
    document.querySelector("#align_raw_change_points").click();
    await new Promise(resolve => setTimeout(resolve, 1000));
    const historyAfter = detailPlot.data
        .filter(trace => trace.meta?.comparison_role === "history")
        .map(trace => Number(trace.x[0]));
    const historyDeltas = historyAfter.map((value, index) => value - historyBefore[index]);
    return {
        compactTraceCount: compactPlot.data.length,
        compactHasCurrentOnlyTitle: compactTitles.some(title => String(title).includes("当前变化 / 历史未检出变化")),
        status: document.querySelector("#signal_raw_comparison_detail_status")?.textContent.trim(),
        detailTraceCount: detailPlot.data.length,
        distanceHistoryReferenceBaseline: distanceHistory.meta?.reference_baseline === true,
        configuredGroupShift: Number(distanceHistory.meta?.alignment_shift_seconds || 0),
        animatedHistoryDeltas: historyDeltas,
        allHistoryTracesMovedTogether: historyDeltas.every(delta => Math.abs(delta - historyDeltas[0]) < 0.0001),
        referenceBaselineMoved: Math.abs(historyDeltas.at(-1)) > 0.001,
        sharedTraceAnimated: Math.abs(historyDeltas[0]) > 0.001
    };
})()`;
const result = await call("Runtime.evaluate", {expression, awaitPromise: true, returnByValue: true});
if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
process.stdout.write(JSON.stringify(result.result.value, null, 2) + "\n");
socket.close();
