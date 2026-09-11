import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    // A CONST READ BEFORE ITS DECLARATION IS A CRASH, NOT A STYLE
    // ISSUE.
    //
    // Field repro 2026-09-11: the Speakers tab read a useState value
    // one line above the useState call that created it. Every render
    // threw "Cannot access 'gone' before initialization" and took the
    // whole window down with it — the app showed WebView2's own "This
    // page couldn't load".
    //
    // `tsc --noEmit` passed. It will not flag a reference inside a
    // closure, because a closure COULD be invoked after the
    // declaration — and there is no way for it to know that
    // `.filter()` invokes this one immediately. ESLint checks the
    // reference position rather than reasoning about when the closure
    // runs, which is exactly the distinction that matters here.
    //
    // Functions stay exempt: hoisted function declarations are a
    // normal and readable ordering, and flagging them would bury the
    // signal that matters under noise.
    rules: {
      "no-use-before-define": "off",
      "@typescript-eslint/no-use-before-define": ["error", {
        functions: false,
        classes: true,
        variables: true,
        enums: true,
        typedefs: false,
        ignoreTypeReferences: true,
      }],
    },
  },
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Self-contained Capacitor sub-project with its own tsconfig +
    // node_modules; the desktop lint/typecheck must not pull it in.
    "mobile/**",
  ]),
]);

export default eslintConfig;
