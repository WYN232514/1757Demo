const targets = await (await fetch("http://127.0.0.1:9223/json/list")).json();
const page = targets.find(target => target.type === "page" && target.url.includes("127.0.0.1:18050"));
const socket = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, {once: true});
    socket.addEventListener("error", reject, {once: true});
});
const result = await new Promise((resolve, reject) => {
    socket.addEventListener("message", event => resolve(JSON.parse(event.data)), {once: true});
    socket.send(JSON.stringify({
        id: 1,
        method: "Runtime.evaluate",
        params: {
            returnByValue: true,
            expression: `({
                compactStatus: document.querySelector("#signal_raw_comparison_compact_status")?.textContent,
                detailStatus: document.querySelector("#signal_raw_comparison_detail_status")?.textContent,
                modalOpen: document.querySelector("#signal_comparison_dashboard_modal")?.classList.contains("is-open"),
                alignDisabled: document.querySelector("#align_raw_change_points")?.disabled,
                compactTraces: document.querySelector("#signal_raw_comparison_compact .js-plotly-plot")?.data?.length,
                detailTraces: document.querySelector("#signal_raw_comparison_detail .js-plotly-plot")?.data?.length
            })`
        }
    }));
});
process.stdout.write(JSON.stringify(result.result.result.value, null, 2) + "\n");
socket.close();
