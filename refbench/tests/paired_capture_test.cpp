#include "paired_capture.h"
#include <cassert>
#include <iostream>
using namespace paired_capture;
int main(int argc, char **argv) {
  assert(digest("", 0) ==
         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  assert(digest("abc", 3) ==
         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  std::string large(1000000, 'a');
  Sha256 split;
  for (size_t i = 0; i < large.size(); i += 17)
    split.update(large.data() + i, std::min(size_t(17), large.size() - i));
  assert(split.finish() ==
         "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
  uint64_t frozen = 0;
  assert(
      FrozenTimeParser(
          "{\"nested\":[null,true,{\"x\":-1.2e3}],\"frozen_at_unix_ms\":123}")
          .parse(&frozen) &&
      frozen == 123);
  for (const char *bad :
       {"{}", "{\"frozen_at_unix_ms\":0}", "{\"frozen_at_unix_ms\":-1}",
        "{\"frozen_at_unix_ms\":1.0}", "{\"frozen_at_unix_ms\":1e3}",
        "{\"frozen_at_unix_ms\":18446744073709551616}",
        "{\"frozen_at_unix_ms\":1,\"frozen_at_unix_ms\":2}",
        "{\"frozen_at_unix_ms\":1,\"x\":01}",
        "{\"frozen_at_unix_ms\":1}trailing",
        "{\"frozen_at_unix_ms\":1,\"x\":false,\"x\":true}",
        "{\"frozen_at_unix_ms\":1,\"\\u0078\":true}"})
    assert(!FrozenTimeParser(bad).parse(&frozen));
  assert(name_ok("run-123") && !name_ok("unset") && !name_ok("../escape"));
  assert(hash_ok(std::string(64, 'a')) && !hash_ok(std::string(64, 'A')));
  assert(phase_bits(phase(30, 60)) == 0x40490fdb);
  if (argc != 4) {
    std::cout << "sha/parser unit checks passed\n";
    return 0;
  }
  std::string rb, rh, ch;
  assert(file_hash(argv[2], &rh, &rb) && FrozenTimeParser(rb).parse(&frozen));
  assert(file_hash(argv[3], &ch));
  Config c;
  c.run_id = "host_fixture";
  c.rules_sha256 = rh;
  c.scene_shader_sha256 = digest("synthetic_scene", 15);
  c.vertex_shader_sha256 = digest("synthetic_vertex", 16);
  c.candidate_sha256 = ch;
  c.frozen_at_ms = frozen;
  c.source = "synthetic_fixture";
  c.count = 4;
  Sink future;
  Config f = c;
  f.run_id = "future";
  f.frozen_at_ms = clock_ns(CLOCK_REALTIME) / 1000000 + 100000;
  assert(!future.begin(argv[1], f));
  Sink sink;
  assert(sink.begin(argv[1], c));
  Sink duplicate;
  assert(!duplicate.begin(argv[1], c));
  std::vector<uint8_t> lo(4 * 2 * 4, 255), hi(8 * 4 * 4, 255);
  std::vector<Raw> raw = {{"native_lr", 4, 2, lo.data()},
                          {"native_hr", 8, 4, hi.data()},
                          {"postfx_candidate", 8, 4, hi.data()}};
  assert(!sink.frame(1, phase(1, 60), clock_ns(CLOCK_MONOTONIC), raw));
  assert(!sink.frame(0, phase(1, 60), clock_ns(CLOCK_MONOTONIC), raw));
  assert(!sink.frame(0, phase(0, 60), 0, raw));
  auto wrong = raw;
  wrong[0].role = "native_hr";
  assert(!sink.frame(0, phase(0, 60), clock_ns(CLOCK_MONOTONIC), wrong));
  for (int frame = 0; frame < 4; frame++) {
    for (size_t i = 0; i < hi.size(); i += 4) {
      hi[i] = uint8_t(30 + frame * 12 + (i / 4) % 7);
      hi[i + 1] = uint8_t(40 + frame * 12);
      hi[i + 2] = uint8_t(50 + frame * 12);
    }
    uint64_t now = clock_ns(CLOCK_MONOTONIC);
    assert(sink.frame(frame, phase(frame, 60), now, raw));
    assert(!sink.frame(frame, phase(frame, 60), now, raw));
  }
  assert(sink.manifest(true));
  Config inc = c;
  inc.run_id = "incomplete";
  Sink incomplete;
  assert(incomplete.begin(argv[1], inc));
  assert(incomplete.frame(0, phase(0, 60), clock_ns(CLOCK_MONOTONIC), raw));
  assert(incomplete.manifest(true));
  Config failed_cfg = c;
  failed_cfg.run_id = "failed";
  Sink failed;
  assert(failed.begin(argv[1], failed_cfg));
  assert(failed.frame(0, phase(0, 60), clock_ns(CLOCK_MONOTONIC), raw));
  assert(failed.fail("injected_submission_fence_timeout"));
  assert(failed.manifest(true)); // sticky failure must never become complete
  assert(!failed.frame(1, phase(1, 60), clock_ns(CLOCK_MONOTONIC), raw));
  Config link_cfg = c;
  link_cfg.run_id = "symlink_guard";
  Sink link_guard;
  assert(link_guard.begin(argv[1], link_cfg));
  std::string link = link_guard.directory() + "/0_native_lr.rgba";
  assert(symlink(argv[3], link.c_str()) == 0);
  assert(!link_guard.frame(0, phase(0, 60), clock_ns(CLOCK_MONOTONIC), raw));
  assert(link_guard.fail("output_collision"));
  std::cout << sink.directory() << "\n";
}
