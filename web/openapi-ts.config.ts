import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "openapi.json",
  output: "src/api",
  // The spec lists no servers; requests stay same-origin (the dev server proxies /api).
  plugins: [{ name: "@hey-api/client-fetch", baseUrl: false }],
});
