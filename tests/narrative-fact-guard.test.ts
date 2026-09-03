import { describe, expect, it } from "vitest";
import { findNarrativeFactViolations } from "../src/domain/co-creation/narrative-fact-guard.js";
import type { BranchState } from "../src/domain/co-creation/branch-state.js";

const lockedTunnelState: BranchState = {
  playerLocationId: "location_signal_tunnel",
  tangLocationId: "location_signal_tunnel",
  jiangLocationId: "location_signal_tunnel",
  tangStatus: "located",
  hasLockerToken: true,
  evidenceStatus: "unsecured",
  waterLevel: "rising",
  signalRoomStatus: "locked",
  trainStatus: "pending_release",
};

describe("narrative fact guard", () => {
  it("rejects crossing a signal-room door that remains locked", () => {
    const violations = findNarrativeFactViolations(
      "许川侧身挤过门缝，应急灯扫过狭窄的隔间，只见唐栖蜷在配电箱旁。",
      lockedTunnelState,
    );

    expect(violations).toContainEqual(expect.objectContaining({ code: "signal_room_locked_crossed" }));
  });

  it("rejects other unconfirmed state claims while allowing external observation", () => {
    const violations = findNarrativeFactViolations(
      "许川隔着门缝听见唐栖的声音，随后把录音笔贴身收好。积水已经退去，唐栖被救出并回到候车厅，末班列车随即驶离。",
      lockedTunnelState,
    );

    expect(violations.map((violation) => violation.code)).toEqual(expect.arrayContaining([
      "evidence_unsecured_claimed",
      "tang_not_rescued_departed",
      "water_still_rising_lowered",
      "train_pending_moved",
    ]));
  });

  it("does not reject the same facts after their state changes are confirmed", () => {
    const violations = findNarrativeFactViolations(
      "许川带着录音笔回到候车厅，唐栖已经获救。积水正在退去，末班列车仍停在站台。",
      {
        ...lockedTunnelState,
        playerLocationId: "location_waiting_hall",
        tangLocationId: "location_waiting_hall",
        jiangLocationId: "location_waiting_hall",
        tangStatus: "rescued",
        evidenceStatus: "secured",
        waterLevel: "lowered",
        signalRoomStatus: "opened",
        trainStatus: "held",
      },
    );

    expect(violations).toEqual([]);
  });
});
