import "@testing-library/jest-dom/vitest";

// jsdom's AbortSignal is not the one Node's fetch `Request` accepts (react-router passes
// one on every navigation): drop the signal in tests, navigation is not aborted anyway.
const NodeRequest = globalThis.Request;
globalThis.Request = class extends NodeRequest {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    if (init?.signal) {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { signal, ...rest } = init;
      super(input, rest);
    } else {
      super(input, init);
    }
  }
} as typeof Request;
