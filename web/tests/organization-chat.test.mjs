import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server, api, TopicNavigation, automaticModelAttempt, prepareMessageAttempt, initializeDraft, agentCommand, alphaMessageGroups, MessageMarkdown;
before(async () => {
  server = await createServer({ appType: "custom", logLevel: "silent", server: { middlewareMode: true } });
  api = await server.ssrLoadModule("/src/organization-api.ts");
  ({ TopicNavigation, automaticModelAttempt, prepareMessageAttempt, initializeDraft, agentCommand, alphaMessageGroups, MessageMarkdown } = await server.ssrLoadModule("/src/OrganizationApp.tsx"));
});
after(async () => { await server?.close(); });

test("private projects say when they are visible through an organization owner or admin role", () => {
  const hierarchy = { projects: [
      { id: "mine", name: "Roster", visibility: "private", project_role: "member" },
      { id: "theirs", name: "Developers", visibility: "private" },
      { id: "open", name: "Algebra", visibility: "organization" }],
    channels: [], topics: [], topic_state: [] };
  const markup = renderToStaticMarkup(React.createElement(TopicNavigation, {
    hierarchy, organizationId: "org", selected: null, canCreate: true, canManageOrganization: true, onSelect: () => {},
    onCreateChannel: () => {}, onCreateTopic: () => {}, onProjectSettings: () => {}, onChannelSettings: () => {},
  }));
  assert.equal((markup.match(/private · visible to you as admin/g) ?? []).length, 1, "only the project seen through the organization role");
  assert.match(markup, /aria-label="Private project, visible to you as an organization owner or admin"/);
  assert.equal((markup.match(/aria-label="Private project"/g) ?? []).length, 1, "the rostered project keeps the plain marker");
});

test("first send gets a stable retry identifier before asynchronous draft initialization", () => {
  const attempt = prepareMessageAttempt({ text: "Immediate first message", clientId: "", attempted: false });
  assert.match(attempt.clientId, /^[0-9a-f-]{36}$/);
  assert.equal(prepareMessageAttempt(attempt).clientId, attempt.clientId);
  assert.equal(attempt.text, "Immediate first message");
  assert.equal(attempt.attempted, true);
});

test("late draft initialization never overwrites already-typed text", () => {
  const existing = { topic: { text: "Already typed", clientId: "saved-id", attempted: false } };
  assert.equal(initializeDraft(existing, "topic"), existing);
  assert.equal(initializeDraft(existing, "another").topic, existing.topic);
});

test("navigation nests creation controls and renders colored channel and topic labels", () => {
  const hierarchy = {
    projects: [{ id: "p", name: "Algebra" }], channels: [
      { id: "c", project_id: "p", name: "research" },
      { id: "d", project_id: "p", name: "notes" },
    ],
    topics: ["alpha", "beta", "gamma"].map((kind) => ({ id: kind, channel_id: "c", kind, name: kind + " conversation" })),
    topic_state: [{ topic_id: "alpha", subscribed: true, unread_count: 3, unread_mention_count: 1 },
      { topic_id: "beta", subscribed: false, unread_count: 5, unread_mention_count: 0 }],
  };
  const markup = renderToStaticMarkup(React.createElement(TopicNavigation, {
    hierarchy, organizationId: "org fixture", selected: "alpha", canCreate: true, canManageOrganization: true, onSelect: () => {}, onCreateChannel: () => {}, onCreateTopic: () => {},
    onProjectSettings: () => {}, onChannelSettings: () => {},
  }));
  assert.match(markup, /Algebra/); assert.match(markup, /#research/); assert.match(markup, /#notes/);
  assert.doesNotMatch(markup, /Project work|Work in Algebra|view=work/);
  assert.match(markup, /aria-label="New channel in Algebra"/); assert.match(markup, /aria-label="New topic in #research"/);
  assert.match(markup, /aria-label="Settings for Algebra"/); assert.match(markup, /aria-label="Settings for #research"/);
  assert.match(markup, /org-channel org-tree-tone-0/); assert.match(markup, /org-channel org-tree-tone-1/);
  assert.match(markup, /org-topic org-tree-tone-0/); assert.match(markup, /org-topic org-tree-tone-1/);
  assert.doesNotMatch(markup, /<summary title="research"/); assert.match(markup, /class="org-channel-name"[^>]*aria-expanded="true"/);
  assert.match(markup, /α<\/span><span class="org-topic-name">-alpha conversation<\/span>/);
  for (const kind of ["alpha", "beta", "gamma"]) assert.match(markup, new RegExp(`aria-label="${kind} topic"`));
  assert.match(markup, /aria-current="page"/);
  assert.match(markup, /3 unread messages/); assert.match(markup, /1 unread personal mentions/);
  assert.doesNotMatch(markup, /5 unread messages/); assert.match(markup, /org-unfollowed/);
});
test("alpha messages group only adjacent same-author messages within five minutes on one day", () => {
  const message = (id, author_id, created_at, author_kind="human") => ({id,author_id,created_at,author_kind});
  const messages = [message("a","alice",100),message("b","alice",150),message("c","bob",160),
    message("d","alice",180),message("e","alice",600),message("f","alice",601,"assistant"),message("g","alice",602,"assistant")];
  assert.deepEqual(alphaMessageGroups(messages).map(group=>group.map(item=>item.id)), [["a","b"],["c"],["d"],["e"],["f"],["g"]]);
  const midnight = new Date(2026,9,9).getTime()/1000;
  assert.equal(alphaMessageGroups([message("x","alice",midnight-1),message("y","alice",midnight+1)]).length,2);
});

test("message reconciliation retains intervening messages and removes replay duplicates", () => {
  const one = { id: "one", sequence: 1 }, two = { id: "two", sequence: 2 }, three = { id: "three", sequence: 3 };
  assert.deepEqual(api.mergeMessages([one, three], [one, two]), [one, two, three]);
  const revised = { ...one, content: "An edited prompt", revision: 1 };
  assert.deepEqual(api.mergeMessages([revised], [one]), [revised], "a stale poll cannot replace a saved edit");
  assert.deepEqual(api.mergeMessages([one], [revised]), [revised]);
});

test("message reconciliation keeps unchanged identities so polling does not rebuild the transcript", () => {
  const one = { id: "one", sequence: 1, content: "$x$" }, two = { id: "two", sequence: 2, content: "$y$" }, existing = [one, two];
  assert.equal(api.mergeMessages(existing, []), existing, "an empty delta returns the same array");
  assert.equal(api.mergeMessages(existing, [{ ...one }, { ...two }]), existing, "an equal replay returns the same array");
  const usage = { ...two, usage: { prompt_tokens: 3 } }, merged = api.mergeMessages(existing, [usage]);
  assert.notEqual(merged, existing); assert.equal(merged[0], one, "unchanged messages keep their objects"); assert.equal(merged[1], usage);
});

test("message Markdown renders again only when its source text changes", () => {
  assert.equal(MessageMarkdown.$$typeof, Symbol.for("react.memo"));
  assert.match(renderToStaticMarkup(React.createElement(MessageMarkdown, { markdown: "Area $\\pi r^2$" })), /katex/);
});

test("agent controls are exact commands rather than paid inference requests", () => {
  assert.equal(agentCommand(" /pause agents "), "paused"); assert.equal(agentCommand("/resume"), "active");
  assert.equal(agentCommand("/STOP"), "cancelled");
  for (const text of ["Explain /pause", "/stopwatch", "/resume something else", "normal conversation"])
    assert.equal(agentCommand(text), null);
});

test("beta default replies capture one model and the exact message boundary", () => {
  assert.deepEqual(automaticModelAttempt("provider/model", 17, 131072, "stable-id"), {
    client_id: "stable-id", models: ["provider/model"], max_completion_tokens: null,
    context_message_limit: 50, through_sequence: 17,
  });
  assert.equal(automaticModelAttempt("provider/model", 17, 943718, "kimi").max_completion_tokens, null);
  assert.equal(automaticModelAttempt("provider/model", 17, 1024, "small").max_completion_tokens, null);
});

test("mention completion only matches an active @ fragment, not emails or encoded mentions", () => {
  assert.deepEqual(api.mentionQuery("Hello @Ow", 9), { start: 6, end: 9, query: "Ow" });
  assert.deepEqual(api.mentionQuery("@", 1), { start: 0, end: 1, query: "" });
  assert.deepEqual(api.mentionQuery("(@Re)", 4), { start: 1, end: 4, query: "Re" });
  for (const text of ["user@example.org", `@[${"a".repeat(32)}] `, "@Owner ", "no mention"]) assert.equal(api.mentionQuery(text, text.length), null);
  assert.equal(api.mentionQuery("@Ow", 99), null);
});

test("account menu contains account and organization options behind the account name", async () => {
  const { AccountMenu } = await server.ssrLoadModule("/src/OrganizationApp.tsx");
  const user = { id: "u", name: "My account", email: "private@example.org" };
  const organization = { id: "o", name: "Org", role: "owner" };
  const markup = renderToStaticMarkup(React.createElement(AccountMenu, {
    user, organization, organizations: [organization], canCreateOrganization: true,
  }));
  assert.match(markup, /My account/); assert.match(markup, /aria-expanded="false"/);
  for (const label of ["User information", "Information", "Administration", "New organization", "Model capabilities", "Usage and energy", "Sign out"]) assert.match(markup, new RegExp(label));
  assert.doesNotMatch(markup, /Shared knowledge/);
  assert.match(markup, /private@example.org/);
  const member = renderToStaticMarkup(React.createElement(AccountMenu, {
    user, organization: { ...organization, role: "viewer" }, organizations: [organization], canCreateOrganization: false,
  }));
  assert.doesNotMatch(member, /Administration|New organization/);
});

test("usage labels distinguish unknown values from explicitly estimated zero and expose assumptions", async () => {
  const { estimateLabel, MessageUsageDetails, OrganizationModelBrowser, OrganizationUsagePanel } = await server.ssrLoadModule("/src/OrganizationUsage.tsx");
  assert.equal(estimateLabel(null), "—"); assert.equal(estimateLabel(0), "0");
  const markup = renderToStaticMarkup(React.createElement(MessageUsageDetails, { usage: {
    prompt_tokens: 20, completion_tokens: 5, actual_microusd: 8, estimated_kwh: 0.0007, estimated_kg_co2e: 0.00028,
    assumptions: { location: "Assumed region", input_kwh_per_million: 10, output_kwh_per_million: 100, grid_grams_co2e_per_kwh: 400, source: "Test scenario" },
  } }));
  assert.match(markup, /20 in \/ 5 out tokens/); assert.match(markup, /0.7 Wh/); assert.match(markup, /0.28 g CO₂/);
  assert.match(markup, /Test scenario/); assert.doesNotMatch(markup, /<details|<summary/);
  const unavailable = renderToStaticMarkup(React.createElement(MessageUsageDetails, { usage: { prompt_tokens: null, completion_tokens: null, estimated_kwh: null, estimated_kg_co2e: null } }));
  assert.doesNotMatch(unavailable, /unknown kWh|unknown kg|≈.*kg CO₂e/);
  const browser = renderToStaticMarkup(React.createElement(OrganizationModelBrowser, { organizationId: "org" }));
  assert.match(browser, /Model capabilities/); assert.match(browser, /Capability/); assert.match(browser, /org-model-rows/);
  const viewer = renderToStaticMarkup(React.createElement(OrganizationUsagePanel, { organizationId: "org", canManage: false }));
  assert.doesNotMatch(viewer, /Save model assumptions/);
});

test("mention labels are text, not member-supplied Markdown", () => {
  const id = "a".repeat(32);
  const members = [{ id, name: "[unsafe](javascript:alert(1)) $x$" }];
  assert.equal(api.mentionText(`Hello @[${id}]`, members), "Hello @[unsafe](javascript:alert(1)) $x$");
  const safe = api.mentionMarkdown(`Hello @[${id}]`, members);
  assert.match(safe, /@\\\[unsafe\\\]/);
  assert.match(safe, /\\\$x\\\$/);
  assert.equal(api.mentionText(`@[${"b".repeat(32)}]`, members), "@departed member");
});

test("budget parsing preserves microusd and rejects negative or imprecise amounts", () => {
  assert.equal(api.decimalBudget("50.000001"), 50000001);
  assert.equal(api.decimalBudget("0"), 0);
  for (const value of ["-1", "NaN", "0.0000001", "1e6", "1000000001"]) assert.throws(() => api.decimalBudget(value));
});

test("cost display preserves small provider charges", () => {
  assert.equal(api.usd(8), "$0.000008");
  assert.equal(api.usd(1000000), "$1.000000");
});

test("invitation email statuses distinguish provider acceptance from inbox delivery", () => {
  assert.match(api.invitationEmailLabel({ email_state: "accepted" }), /inbox delivery unconfirmed/);
  assert.match(api.invitationEmailLabel({ email_state: "unknown" }), /no automatic resend/);
  assert.match(api.invitationEmailLabel({ email_state: "rejected" }), /share the invitation link/);
  assert.equal(api.invitationEmailLabel({ email_state: null }), "Link sharing only");
  for (const state of ["queued", "sending", "cancelled"]) assert.ok(api.invitationEmailLabel({ email_state: state }));
});

test("bot senders cannot be displayed as human members or LLM participants", () => {
  const members = [{ id: "human", name: "Human name" }];
  assert.equal(api.authorLabel({ author_kind: "human", author_id: "human" }, members), "Human name");
  assert.equal(api.authorLabel({ author_kind: "bot", author_id: "human", bot_name: "Research" }, members), "Bot · Research");
  assert.equal(api.authorLabel({ author_kind: "assistant", assistant_model: "provider/model" }, members), "LLM · provider/model");
});

test("bot grants identify the project, channel, and topic rather than ambiguous names", () => {
  const tree = { projects: [{ id: "p", name: "Algebra" }], channels: [{ id: "c", name: "research", project_id: "p" }],
    topics: [{ id: "t", name: "Examples", channel_id: "c", kind: "alpha" }] };
  assert.equal(api.topicGrantLabel(tree, "t"), "Algebra / #research / α-Examples");
  assert.equal(api.topicGrantLabel(tree, "missing"), "missing");
});

test("bot onboarding explains external execution and defaults to scoped notification access", async () => {
  const { BotAdministration } = await server.ssrLoadModule("/src/BotAdministration.tsx");
  const markup = renderToStaticMarkup(React.createElement(BotAdministration, { organizationId: "organization" }));
  assert.match(markup, /Organization bots/); assert.match(markup, /Custom bot \/ manual credential/);
  assert.match(markup, /Bots run outside Reasoning/); assert.match(markup, /Send updates/);
  assert.match(markup, /Watch and respond/); assert.match(markup, /Read events/);
  assert.match(markup, /Knowledge access/); assert.match(markup, /Read and publish/);
  assert.match(markup, /Restricted topics/); assert.match(markup, /30 days \(recommended\)/);
  assert.match(markup, /Create bot and show credential/); assert.match(markup, /disabled/);
  assert.doesNotMatch(markup, /select multiple|messages:read|messages:write|localStorage|sessionStorage/);
});

test("personal bots lead with CLI agents and are grouped underneath their attached person", async () => {
  const { BotAdministration } = await server.ssrLoadModule("/src/BotAdministration.tsx");
  const { peopleWithBots } = await server.ssrLoadModule("/src/OrganizationApp.tsx");
  const markup = renderToStaticMarkup(React.createElement(BotAdministration, { organizationId: "organization", personal: true }));
  assert.match(markup, /Codex, Claude Code/);
  assert.match(markup, /Follow my access, including private projects and new topics/);
  assert.match(markup, /rechecks your current permissions on every request/);
  assert.doesNotMatch(markup, /Outgoing webhooks/);
  const people = peopleWithBots({ humans: [{ id: "person", name: "Owner", online: false }],
    bots: [{ id: "bot", name: "Codex", owner_user_id: "person", online: true }] });
  assert.equal(people.length, 1); assert.equal(people[0].bots[0].id, "bot");
});

test("CSRF cookies prefer the HTTPS host-bound credential and never use session cookies", () => {
  assert.equal(api.csrfCookie("reasoning_csrf=local; __Host-reasoning_csrf=secure; reasoning_session=secret"), "secure");
  assert.equal(api.csrfCookie("__Host-reasoning_session=secret"), null);
  assert.equal(api.csrfCookie("reasoning_csrf=%broken"), null);
});

test("browser requests use same-origin cookies and CSRF, not browser bearer storage", async () => {
  const previousFetch = globalThis.fetch;
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: { cookie: "__Host-reasoning_csrf=csrf-value" } });
  globalThis.fetch = async (path, options) => {
    assert.equal(path, "/api/organizations"); assert.equal(options.credentials, "same-origin");
    assert.equal(options.headers["X-CSRF-Token"], "csrf-value"); assert.equal(options.headers.Authorization, undefined);
    assert.equal(options.body, '{"name":"Organization"}');
    return new Response('{"id":"organization"}', { status: 201 });
  };
  try { assert.deepEqual(await api.request("/organizations", { method: "POST", body: { name: "Organization" } }), { id: "organization" }); }
  finally {
    globalThis.fetch = previousFetch;
    if (previousDocument) Object.defineProperty(globalThis, "document", previousDocument); else delete globalThis.document;
  }
});
