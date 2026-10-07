import { joinSession, createCanvas, CanvasError } from "@github/copilot-sdk/extension";
import { PrDashboard } from "./pr-dashboard.mjs";
import { startServer } from "./server.mjs";
import { REPOSITORIES } from "./repositories.mjs";
import { KIND_LABELS } from "./kinds.mjs";

const dashboard = new PrDashboard();
const servers = new Map();
const emptyInput = { type: "object", properties: {}, additionalProperties: false };
const target = { type: "string", pattern: `^(?:${REPOSITORIES.join("|")})#[1-9][0-9]{0,7}$` };
const confirmed = { type: "boolean", const: true, description: "The user explicitly requested this target and action. A direct button click is sufficient." };

function action(name, description, properties, required, handler) {
    return {
        name, description,
        inputSchema: { type: "object", properties, required, additionalProperties: false },
        handler: async (ctx) => {
            try {
                return await handler(ctx.input);
            } catch (error) {
                throw new CanvasError(`pr_${name}_failed`, error.message);
            }
        },
    };
}

const cancelAction = action("cancel", "Cancel an exact accepted launch run or observed nonterminal task on explicit user request. Does not undo publication.",
    { target, runId: { type: "integer", minimum: 1 },
        requestId: { type: "string", pattern: "^[0-9a-f]{32}$" },
        generation: { type: "integer", minimum: 1 }, confirmed },
    ["target", "confirmed"], (input) => dashboard.cancel(input));
cancelAction.inputSchema.oneOf = [
    { required: ["runId"], not: { anyOf: [{ required: ["requestId"] }, { required: ["generation"] }] } },
    { required: ["requestId", "generation"], not: { required: ["runId"] } },
];

await joinSession({
    canvases: [createCanvas({
        id: "workflow-dashboard",
        displayName: "PR workflows",
        description: "Browse open repository PRs, see needed work, filter by author or reviewer routing, and run or cancel central Actions tasks.",
        inputSchema: { type: "object", properties: { repo: { type: "string", enum: REPOSITORIES } }, additionalProperties: false },
        actions: [
            {
                name: "refresh",
                description: "Refresh open PRs, reviewer routing, live action evidence and central task status without starting workflows.",
                inputSchema: emptyInput,
                handler: async () => {
                    const state = await dashboard.refresh();
                    if (state.error || state.prError) throw new CanvasError("dashboard_refresh_failed", state.prError || state.error);
                    return state;
                },
            },
            action("select_repository", "Choose a configured repository and refresh its open PRs.",
                { repo: { type: "string", enum: REPOSITORIES } }, ["repo"], async (input) => {
                    const state = await dashboard.selectRepository(input.repo);
                    if (state.prError) throw new Error(state.prError);
                    return state;
                }),
            action("launch", "Dispatch the selected PR task on explicit user request, without a separate confirmation dialog.",
                { target, kind: { type: "string", enum: Object.keys(KIND_LABELS) }, confirmed },
                ["target", "kind", "confirmed"], (input) => dashboard.launch(input)),
            cancelAction,
            {
                name: "history",
                description: "Read saved iteration history and previous phases for a dashboard target.",
                inputSchema: {
                    type: "object",
                    properties: { target: { type: "string", pattern: "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]{0,7}$" } },
                    required: ["target"],
                    additionalProperties: false,
                },
                handler: async (ctx) => {
                    try {
                        return await dashboard.history(ctx.input.target);
                    } catch (error) {
                        throw new CanvasError("dashboard_history_failed", error.message);
                    }
                },
            },
        ],
        open: async (ctx) => {
            if (ctx.input?.repo && ctx.input.repo !== dashboard.repository) await dashboard.selectRepository(ctx.input.repo);
            let entry = servers.get(ctx.instanceId);
            if (!entry) {
                entry = await startServer(dashboard);
                servers.set(ctx.instanceId, entry);
            }
            return { title: "PR workflows", url: entry.url };
        },
        onClose: async (ctx) => {
            const entry = servers.get(ctx.instanceId);
            if (entry) {
                servers.delete(ctx.instanceId);
                await entry.close();
            }
        },
    })],
});
