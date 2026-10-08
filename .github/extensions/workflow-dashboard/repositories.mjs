export const REPOSITORIES = [
    "open-telemetry/opentelemetry-java-instrumentation",
    "open-telemetry/semantic-conventions-conformance",
    "open-telemetry/shared-workflows",
    "open-telemetry/semantic-conventions-genai",
    "open-telemetry/semantic-conventions",
    "open-telemetry/github-threat-detection",
    "open-telemetry/admin",
];
export const DEFAULT_REPOSITORY = REPOSITORIES[0];
export const LAUNCH_OWNER_ID = 218610;

export function configuredRepository(repo) {
    if (!REPOSITORIES.includes(repo)) throw new Error("Select a configured repository from the dropdown.");
    return repo;
}

export function dashboardPath(repo) {
    const name = configuredRepository(repo).split("/")[1];
    return `repos/open-telemetry/shared-workflows/contents/${name}/dashboard-state.json?ref=${encodeURIComponent(`otelbot/pull-request-dashboard-state/${name}`)}`;
}

export function targetParts(target) {
    const match = typeof target === "string" && /^(.+)#([1-9][0-9]{0,7})$/.exec(target);
    if (!match) throw new Error("Use a configured owner/repo#number.");
    return { repo: configuredRepository(match[1]), number: Number(match[2]) };
}
