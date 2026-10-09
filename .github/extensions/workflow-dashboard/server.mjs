import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";

const assets = new Map([
    ["/", ["index.html", "text/html; charset=utf-8"]],
    ["/app.mjs", ["app.mjs", "text/javascript; charset=utf-8"]],
    ["/kinds.mjs", ["kinds.mjs", "text/javascript; charset=utf-8"]],
    ["/prs.mjs", ["prs.mjs", "text/javascript; charset=utf-8"]],
    ["/repositories.mjs", ["repositories.mjs", "text/javascript; charset=utf-8"]],
    ["/styles.css", ["styles.css", "text/css; charset=utf-8"]],
]);

async function body(req) {
    if (req.headers["content-type"]?.split(";")[0].trim() !== "application/json") {
        throw new Error("Canvas actions require an application/json body.");
    }
    if (Number(req.headers["content-length"]) > 4096) {
        req.resume();
        throw new Error("Canvas action body exceeds 4 KiB.");
    }
    let size = 0;
    const chunks = [];
    await new Promise((resolve, reject) => {
        req.on("data", (chunk) => {
            size += chunk.length;
            if (size <= 4096) chunks.push(chunk);
        });
        req.on("end", resolve);
        req.on("error", reject);
    });
    if (size > 4096) throw new Error("Canvas action body exceeds 4 KiB.");
    let input;
    try {
        input = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
    } catch {
        throw new Error("Canvas action body must be valid UTF-8 JSON.");
    }
    if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("Canvas action body must be an object.");
    return input;
}

export async function startServer(dashboard) {
    const viewer = randomUUID();
    const loaded = new Map();
    for (const [path, [file, type]] of assets) {
        loaded.set(path, { data: await readFile(new URL(file, import.meta.url)), type });
    }
    const server = createServer(async (req, res) => {
        const port = server.address()?.port;
        const origin = `http://127.0.0.1:${port}`;
        res.setHeader("Cache-Control", "no-store");
        res.setHeader("X-Content-Type-Options", "nosniff");
        res.setHeader("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'");
        const json = (status, value) => {
            res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
            res.end(JSON.stringify(value));
        };
        try {
            if (req.headers.host !== `127.0.0.1:${port}` ||
                req.headers.origin && req.headers.origin !== origin ||
                req.headers["sec-fetch-site"] === "cross-site" && !assets.has(new URL(req.url, origin).pathname)) {
                json(403, { error: "Dashboard requests must come from its loopback origin." });
                return;
            }
            const url = new URL(req.url, origin);
            if (req.method === "GET" && loaded.has(url.pathname)) {
                const asset = loaded.get(url.pathname);
                res.writeHead(200, { "Content-Type": asset.type });
                res.end(asset.data);
            } else if (req.method === "GET" && url.pathname === "/api/state") {
                json(200, dashboard.state());
            } else if (req.method === "GET" && url.pathname === "/api/history") {
                const target = url.searchParams.get("target");
                if (!target || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+#[1-9][0-9]{0,7}$/.test(target)) {
                    json(400, { error: "Use an explicit owner/repo#number from the dashboard." });
                } else json(200, await dashboard.history(target));
            } else if (req.method === "POST" && url.pathname === "/api/refresh") {
                const state = await dashboard.refresh();
                json(state.error || state.prError ? 502 : 200, state);
            } else if (req.method === "POST" && url.pathname === "/api/run-log") {
                const state = await dashboard.refreshRunLog();
                json(state.runLogError ? 502 : 200, state);
            } else if (req.method === "POST" && url.pathname === "/api/visibility") {
                const visible = url.searchParams.get("visible");
                if (!["true", "false"].includes(visible)) json(400, { error: "Visibility must be true or false." });
                else {
                    dashboard.heartbeat(viewer, visible === "true");
                    json(200, { visible: visible === "true" });
                }
            } else if (req.method === "POST" && url.pathname === "/api/auto") {
                const enabled = url.searchParams.get("enabled");
                if (!["true", "false"].includes(enabled)) json(400, { error: "Auto refresh must be true or false." });
                else json(200, dashboard.setAuto(enabled === "true"));
            } else if (req.method === "POST" &&
                ["/api/repository", "/api/launch", "/api/cancel"].includes(url.pathname) &&
                typeof dashboard.launch === "function") {
                if (req.headers.origin !== origin) {
                    json(403, { error: "Canvas actions require the canvas's exact origin." });
                    return;
                }
                const input = await body(req);
                if (url.pathname === "/api/repository") {
                    if (Object.keys(input).length !== 1 || typeof input.repo !== "string") {
                        throw new Error("Select exactly one configured repository.");
                    }
                    const state = await dashboard.selectRepository(input.repo);
                    json(state.prError ? 502 : 200, state);
                } else {
                    json(200, await dashboard[url.pathname === "/api/launch" ? "launch" : "cancel"](input));
                }
            } else json(404, { error: "Dashboard endpoint not found." });
        } catch (error) {
            json(400, { error: error.message });
        }
    });
    server.requestTimeout = 10000;
    server.headersTimeout = 10000;
    await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(0, "127.0.0.1", resolve);
    });
    return {
        url: `http://127.0.0.1:${server.address().port}/`,
        close: async () => {
            dashboard.removeViewer(viewer);
            server.closeIdleConnections();
            await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
        },
    };
}
