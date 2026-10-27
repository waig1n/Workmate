import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { expect, test } from "vitest";
import { Dialog } from "./Dialog";

test("Escape closes only the top confirmation and keeps the detail panel open", () => {
  function Example() {
    const [detail, setDetail] = useState(true);
    const [confirmation, setConfirmation] = useState(false);
    return <Dialog open={detail} onClose={() => setDetail(false)} title="详情"><button onClick={() => setConfirmation(true)}>共享</button>
      <Dialog open={confirmation} onClose={() => setConfirmation(false)} title="确认共享"><button>确认</button></Dialog>
    </Dialog>;
  }
  render(<Example />);
  fireEvent.click(screen.getByRole("button", { name: "共享" }));
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "确认共享" })).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "详情" })).toBeVisible();
});
