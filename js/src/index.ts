// @kishibashi3/agent-hub-sdk — public surface for the TypeScript port.
//
// Status: M4 (TypeScript port). Mirrors the Python SDK 1:1.
//
// ```ts
// import { AgentHub, CommandRouter, HubTransientError, PeerNotFoundError } from "@kishibashi3/agent-hub-sdk";
//
// await using hub = await AgentHub.connect({ user: "my-bridge", mode: "stateful" });
// await hub.session.register();
// const router = new CommandRouter();
// router.command("/active", async (msg, hub, args) => `working on ${task}`);
// ```
//
// Everything re-exported below is the stable public surface. Anything
// else is internal and may change without notice.

export { VERSION } from "./version.js";

export { AgentHub, type ConnectOptions } from "./client.js";

export {
  CommandRouter,
  DEFAULT_STATUS,
  parseCommand,
  type CommandHandler,
  type CommandOptions,
  type CommandRouterOptions,
  type DispatchResult,
} from "./commands.js";

export {
  ENV_DISPLAY_NAME,
  ENV_PAT,
  ENV_TENANT,
  ENV_URL,
  makeHeaders,
  resolveConfig,
  type Config,
  type Mode,
  type ResolveConfigOptions,
} from "./config.js";

export {
  ConfigurationError,
  HubTransientError,
  PeerNotFoundError,
  classifyHubError,
  type HubErrorKind,
} from "./errors.js";

export {
  parseMessages,
  parseParticipants,
  type IncomingMessage,
  type Participant,
} from "./messages.js";

export {
  HubSession,
  type HubSessionHandle,
  type InboxOptions,
  type McpClient,
  type McpClientFactory,
  type McpNotification,
  type SendWithRetryOptions,
} from "./session.js";

export {
  extractText,
  raiseForSendError,
  raiseForToolError,
  type ToolContentBlock,
  type ToolContentText,
  type ToolResult,
} from "./transport.js";
