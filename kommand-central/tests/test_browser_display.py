from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node is required for browser-script regression tests")
class BrowserDisplayTests(unittest.TestCase):
    def test_view_and_assignment_updates_preserve_unaffected_video(self):
        node = shutil.which("node")
        assert node is not None
        page = Path(__file__).resolve().parents[1] / "src" / "kommand_central" / "browser_display.html"
        result = subprocess.run(
            [node, "-e", r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor() { this.style = {}; this.children = []; this.hidden = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  removeAttribute(name) { delete this[name]; }
  addEventListener() {}
}
class Socket {
  static OPEN = 1;
  constructor() { this.readyState = Socket.OPEN; this.closed = false; }
  close() { this.closed = true; }
  send() {}
}
class Decoder {
  constructor() { this.state = "unconfigured"; this.decodeQueueSize = 0; }
  configure() { this.state = "configured"; }
  decode() {}
  close() { this.state = "closed"; }
}
const leaf = channel => ({type:"slot", channel, view:{zoom:1, pan_x:0, pan_y:0}});
const snapshot = {
  generation:"original", revision:0, name:"Display",
  layout:{type:"grid", rows:1, columns:2, children:[leaf("source/first"), leaf("source/second")]}
};
const channels = ["first", "second", "third"].map(channel => ({
  source:"source", channel, width:320, height:180, revision:1,
  streaming:true, received_at:new Date().toISOString()
}));
const elements = new Map();
const context = vm.createContext({
  document: {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    createElement() { return new Element(); }
  },
  location:{pathname:"/display/default", protocol:"http:", host:"test"},
  window:{VideoDecoder:Decoder}, VideoDecoder:Decoder, WebSocket:Socket,
  EncodedVideoChunk:class {}, Uint8Array, performance:{now:()=>0},
  AbortSignal:{timeout(){}}, setTimeout(){}, requestAnimationFrame(){return 1;},
  cancelAnimationFrame(){},
  fetch:async url => ({
    ok:true, json:async () => url.includes("/display/") ? structuredClone(snapshot) : {channels}
  })
});
const script = fs.readFileSync(process.argv[1], "utf8").match(/<script>([\s\S]*?)<\/script>/)[1];
vm.runInContext(script, context);

async function check() {
  await new Promise(setImmediate);
  const original = vm.runInContext("slots", context).map(slot => {
    slot.socket.onmessage({data:JSON.stringify({
      keyframe:true, stream:"a".repeat(32), sequence:0, width:320, height:180, revision:1
    })});
    slot.socket.onmessage({data:new Uint8Array([0,0,1,0x67,66,0,30,0,0,1,0x65,128]).buffer});
    slot.canvas.hidden = false;
    slot.status.hidden = true;
    return {slot, canvas:slot.canvas, socket:slot.socket, decoder:slot.decoder};
  });
  for (const pan of [0.1, 0.2, 0.3]) {
    snapshot.layout.children[0].view = {zoom:2, pan_x:pan, pan_y:-0.1};
    snapshot.revision++;
    await vm.runInContext("refresh()", context);
    const updated = vm.runInContext("slots", context);
    original.forEach((previous, index) => {
      assert.equal(updated[index], previous.slot);
      assert.equal(updated[index].canvas, previous.canvas);
      assert.equal(updated[index].socket, previous.socket);
      assert.equal(updated[index].decoder, previous.decoder);
      assert.equal(previous.socket.closed, false);
      assert.equal(previous.decoder.state, "configured");
      assert.equal(updated[index].canvas.hidden, false);
      assert.equal(updated[index].status.hidden, true);
    });
  }
  assert.equal(original[0].canvas.style.transform, "translate(30%, -10%) scale(2)");
  assert.equal(original[1].canvas.style.transform, "translate(0%, 0%) scale(1)");

  snapshot.layout.children[0].channel = "source/third";
  snapshot.revision++;
  await vm.runInContext("refresh()", context);
  const reassigned = vm.runInContext("slots", context);
  assert.equal(reassigned[0].canvas, original[0].canvas);
  assert.notEqual(reassigned[0].socket, original[0].socket);
  assert.equal(original[0].socket.closed, true);
  assert.equal(reassigned[1].socket, original[1].socket);
  assert.equal(reassigned[1].decoder, original[1].decoder);
  assert.equal(reassigned[1].canvas.hidden, false);
  assert.equal(reassigned[0].name.textContent, "Panel 1 / source/third");

  snapshot.generation = "restarted";
  await vm.runInContext("refresh()", context);
  assert.equal(original[1].socket.closed, true);
  assert.notEqual(vm.runInContext("slots[1].canvas", context), original[1].canvas);
}
check().catch(error => {console.error(error); process.exitCode = 1;});
""", str(page)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
