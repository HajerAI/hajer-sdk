import { defineConfig } from "vitest/config";

// Node only: this package has no DOM and no JSX, and a jsdom environment would be a second runtime
// to keep working for nothing.
//
// Fakes only: no test in this package opens a socket, reads a credential or spends money. A run
// against a live Hajer server is `fixture-app.ts`, not a unit test.
export default defineConfig({
  test: {
    environment: "node",
    include: ["test/**/*.test.ts"],
  },
});
