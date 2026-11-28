import { describe, expect, it, vi } from "vitest";
import type { TvPairCode, TvScreenKey } from "@/api/tvTypes";
import { exitApp, KEY, keyAction, registerKeys, TIZEN_KEYS } from "@/tvapp/keys";
import { initialPair, pairReducer, type PairState } from "@/tvapp/pairing";
import { BASE_KEY, loadStored, normalizeBase, saveStored, TOKEN_KEY } from "@/tvapp/storage";
import { fromSelection, NOTES, viewStep, type ViewState } from "@/tvapp/view";

describe("remote keys", () => {
  it("maps the Tizen key codes", () => {
    expect(keyAction({ keyCode: KEY.LEFT })).toBe("prev");
    expect(keyAction({ keyCode: KEY.RIGHT })).toBe("next");
    expect(keyAction({ keyCode: KEY.ENTER })).toBe("toggle-pause");
    expect(keyAction({ keyCode: KEY.PLAY_PAUSE })).toBe("toggle-pause");
    expect(keyAction({ keyCode: KEY.RED })).toBe("health");
    expect(keyAction({ keyCode: KEY.GREEN })).toBe("campaign");
    expect(keyAction({ keyCode: KEY.YELLOW })).toBe("flow");
    expect(keyAction({ keyCode: KEY.BLUE })).toBe("rotation");
    expect(keyAction({ keyCode: KEY.BACK })).toBe("back");
    expect(keyAction({ keyCode: 403 })).toBe("health");
    expect(keyAction({ keyCode: 10009 })).toBe("back");
  });

  it("maps the key names (a desktop browser) and ignores the rest", () => {
    expect(keyAction({ key: "ArrowLeft", keyCode: 0 })).toBe("prev");
    expect(keyAction({ key: "Escape" })).toBe("back");
    expect(keyAction({ key: "XF86Back" })).toBe("back");
    expect(keyAction({ key: "ColorF3Blue" })).toBe("rotation");
    expect(keyAction({ key: "y" })).toBe("flow");
    for (const k of [{ keyCode: 38 }, { keyCode: 40 }, { key: "a" }, { key: "toString" }, {}]) {
      expect(keyAction(k)).toBeNull();
    }
  });

  it("registers the colour keys and Play/Pause; a missing key does not stop the others", () => {
    const got: string[] = [];
    const t = {
      tvinputdevice: {
        registerKey: (n: string) => {
          if (n === "ColorF2Yellow") throw new Error("no such key");
          got.push(n);
        },
      },
    };
    expect(registerKeys(t)).toBe(TIZEN_KEYS.length - 1);
    expect(got).toEqual(["ColorF0Red", "ColorF1Green", "ColorF3Blue", "MediaPlayPause"]);
    expect(registerKeys(undefined)).toBe(0);
  });

  it("Back closes the app on a TV only", () => {
    const exit = vi.fn();
    expect(exitApp({ application: { getCurrentApplication: () => ({ exit }) } })).toBe(true);
    expect(exit).toHaveBeenCalledOnce();
    expect(exitApp(undefined)).toBe(false);
  });
});

describe("the view (portal choice + remote)", () => {
  const all: TvScreenKey[] = ["flow", "campaign", "health"];
  const v = (over: Partial<ViewState> = {}): ViewState => ({ screen: "campaign", rotating: false, paused: false, ...over });

  it("starts from the portal's choice", () => {
    expect(fromSelection({ screen: "health", rotation: true, screens: all })).toEqual(v({ screen: "health", rotating: true }));
    expect(fromSelection({ screen: "flow", rotation: true, screens: ["campaign", "health"] })).toEqual(v({ screen: "campaign", rotating: true }));
    expect(fromSelection({ screen: "campaign", rotation: true, screens: ["campaign"] }).rotating).toBe(false);
  });

  it("arrows move through the allowed screens, wrapping", () => {
    expect(viewStep(v(), "next", all).state.screen).toBe("health");
    expect(viewStep(v({ screen: "health" }), "next", all).state.screen).toBe("flow");
    expect(viewStep(v({ screen: "flow" }), "prev", all).state.screen).toBe("health");
    expect(viewStep(v(), "next", ["campaign"])).toEqual({ state: v(), note: NOTES.oneScreen });
  });

  it("OK pauses and resumes the rotation; the tick moves only a running rotation", () => {
    const r = v({ rotating: true });
    const paused = viewStep(r, "toggle-pause", all);
    expect(paused).toEqual({ state: v({ rotating: true, paused: true }), note: NOTES.paused });
    expect(viewStep(paused.state, "tick", all).state.screen).toBe("campaign");
    const resumed = viewStep(paused.state, "toggle-pause", all);
    expect(resumed.note).toBe(NOTES.resumed);
    expect(viewStep(resumed.state, "tick", all).state.screen).toBe("health");
    expect(viewStep(v(), "toggle-pause", all).note).toBe(NOTES.noRotation);
    expect(viewStep(v(), "tick", all).state.screen).toBe("campaign");
  });

  it("colour keys: red health, green campaign, yellow flow if allowed, blue rotation", () => {
    expect(viewStep(v(), "health", all).state.screen).toBe("health");
    expect(viewStep(v({ screen: "health" }), "campaign", all).state.screen).toBe("campaign");
    expect(viewStep(v(), "flow", all).state.screen).toBe("flow");
    expect(viewStep(v(), "flow", ["campaign", "health"])).toEqual({ state: v(), note: NOTES.flowNotAllowed });
    const on = viewStep(v(), "rotation", all);
    expect(on).toEqual({ state: v({ rotating: true }), note: NOTES.rotationOn });
    const off = viewStep(v({ rotating: true, paused: true }), "rotation", all);
    expect(off).toEqual({ state: v(), note: NOTES.rotationOff });
  });
});

describe("pairing state machine", () => {
  const code: TvPairCode = { poll_id: "p".repeat(32), code: "123456", expires_in_s: 600, poll_s: 2 };
  const base = "http://192.168.30.7:8087";

  it("first start asks the address; a stored token connects; no token shows a code", () => {
    expect(initialPair({ base: null, token: "t" })).toMatchObject({ phase: "setup", token: null });
    expect(initialPair({ base, token: "t" })).toMatchObject({ phase: "connecting", token: "t" });
    expect(initialPair({ base, token: null })).toMatchObject({ phase: "code" });
  });

  it("code → claimed with the token → connecting → live", () => {
    let s: PairState = pairReducer(initialPair({ base: null, token: null }), { type: "configured", base });
    expect(s).toMatchObject({ phase: "code", base });
    s = pairReducer(s, { type: "code", code });
    expect(s.code).toEqual(code);
    s = pairReducer(s, { type: "expired" });
    expect(s).toMatchObject({ phase: "code", code: null });
    s = pairReducer(pairReducer(s, { type: "code", code }), { type: "claimed", token: "tv-display.a.b" });
    expect(s).toMatchObject({ phase: "connecting", token: "tv-display.a.b", code: null });
    s = pairReducer(s, { type: "live" });
    expect(s).toMatchObject({ phase: "live", offline: false });
  });

  it("a refused token (revoked) goes back to the code and forgets it", () => {
    let s = pairReducer(initialPair({ base, token: "old" }), { type: "live" });
    s = pairReducer(s, { type: "unauthorized" });
    expect(s).toMatchObject({ phase: "code", token: null, revoked: true });
    s = pairReducer(pairReducer(s, { type: "code", code }), { type: "claimed", token: "new" });
    expect(s).toMatchObject({ phase: "connecting", token: "new", revoked: false });
  });

  it("claimed without a token: the address is not the app's entry", () => {
    let s = pairReducer(initialPair({ base, token: null }), { type: "code", code });
    s = pairReducer(s, { type: "claimed", token: undefined });
    expect(s).toMatchObject({ phase: "code", token: null, wrongEntry: true });
    s = pairReducer(pairReducer(s, { type: "setup" }), { type: "configured", base: "http://10.0.0.2:8087" });
    expect(s).toMatchObject({ phase: "code", wrongEntry: false, base: "http://10.0.0.2:8087" });
  });

  it("events out of their phase are ignored; a new server forgets the old token", () => {
    const live = pairReducer(initialPair({ base, token: "t" }), { type: "live" });
    expect(pairReducer(live, { type: "code", code })).toBe(live);
    expect(pairReducer(live, { type: "claimed", token: "x" })).toBe(live);
    const codeS = initialPair({ base, token: null });
    expect(pairReducer(codeS, { type: "live" })).toBe(codeS);
    expect(pairReducer(codeS, { type: "unauthorized" })).toBe(codeS);
    expect(pairReducer(pairReducer(live, { type: "setup" }), { type: "configured", base })).toMatchObject({ phase: "connecting", token: "t" });
    expect(pairReducer(pairReducer(live, { type: "setup" }), { type: "configured", base: "http://other:8087" })).toMatchObject({ phase: "code", token: null });
    const off = pairReducer(live, { type: "offline" });
    expect(off.offline).toBe(true);
    expect(pairReducer(off, { type: "online" }).offline).toBe(false);
  });
});

describe("storage and the server address", () => {
  it("normalizes what is typed", () => {
    expect(normalizeBase("192.168.30.7:8087")).toBe("http://192.168.30.7:8087");
    expect(normalizeBase(" http://192.168.30.7:8087/ ")).toBe("http://192.168.30.7:8087");
    expect(normalizeBase("https://smt.lan")).toBe("https://smt.lan");
    for (const bad of ["", "   ", "ftp://x", "http://u:p@x:8087", "http://x:8087/tv", "http://x?a=1", "javascript:alert(1)", null]) {
      expect(normalizeBase(bad)).toBeNull();
    }
  });

  it("keeps the address and the token", () => {
    const m = new Map<string, string>();
    const s = { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) };
    saveStored({ base: "http://h:8087", token: "tok" }, s);
    expect(m.get(BASE_KEY)).toBe("http://h:8087");
    expect(loadStored(s)).toEqual({ base: "http://h:8087", token: "tok" });
    saveStored({ base: "http://h:8087", token: null }, s);
    expect(m.has(TOKEN_KEY)).toBe(false);
    const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("x"); }, removeItem: () => {} };
    expect(loadStored(broken)).toEqual({ base: null, token: null });
    expect(() => saveStored({ base: "http://h", token: "t" }, broken)).not.toThrow();
  });
});
