import { expect, it } from "vitest";

import { buildShareOption } from "./ShareChart";

it("escapes a name in the tooltip, which is HTML", () => {
  const shares = [{ name: "<img src=x onerror=alert(1)>", value: "5.00", color: "#fff" }];
  const option = buildShareOption(shares, (value) => `£${value}`);
  expect(option.tooltip.formatter({ dataIndex: 0 })).toBe(
    "&lt;img src=x onerror=alert(1)&gt;<br/>£5.00",
  );
});
