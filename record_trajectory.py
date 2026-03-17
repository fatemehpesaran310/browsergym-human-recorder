"""
Human Trajectory Recorder for BrowserGym / WebArena-Pro

Records free-form human browser interactions (clicks, typing, scrolling)
and translates them into BrowserGym action strings.

Usage:
    source run_env.sh
    conda activate webarena-pro
    python record_trajectory.py --task_id 0 --output_dir ./trajectories
"""

import argparse
import base64
import json
import os
import time
from pathlib import Path

import gymnasium as gym
import numpy as np
from PIL import Image

import browsergym.webarena_pro
from browsergym.core.observation import _pre_extract, _post_extract
from browsergym.utils.obs import flatten_axtree_to_str


# ---------------------------------------------------------------------------
# JavaScript: event capture + overlay panel
# ---------------------------------------------------------------------------

EVENT_CAPTURE_JS = r"""
(function() {
    if (window.__bgym_recorder_installed) return;
    window.__bgym_recorder_installed = true;
    window.__bgym_events = [];
    window.__bgym_meta_actions = [];
    window.__bgym_typing = {};  // bid -> {value, timeout_id}

    function findBid(el) {
        while (el) {
            if (el.getAttribute && el.getAttribute('bid')) {
                return el.getAttribute('bid');
            }
            el = el.parentElement;
        }
        return null;
    }

    // --- Mouse events ---
    document.addEventListener('click', function(e) {
        // Skip clicks on our overlay panel
        if (e.target.closest && e.target.closest('#__bgym_overlay')) return;
        var bid = findBid(e.target);
        window.__bgym_events.push({
            type: 'click',
            x: e.clientX, y: e.clientY,
            button: e.button,
            bid: bid,
            timestamp: Date.now()
        });
    }, true);

    document.addEventListener('dblclick', function(e) {
        if (e.target.closest && e.target.closest('#__bgym_overlay')) return;
        var bid = findBid(e.target);
        // Remove the last click event on the same bid (dblclick fires after 2 clicks)
        var evts = window.__bgym_events;
        for (var i = evts.length - 1; i >= 0 && i >= evts.length - 3; i--) {
            if (evts[i].type === 'click' && evts[i].bid === bid) {
                evts.splice(i, 1);
            }
        }
        window.__bgym_events.push({
            type: 'dblclick',
            x: e.clientX, y: e.clientY,
            button: e.button,
            bid: bid,
            timestamp: Date.now()
        });
    }, true);

    // --- Keyboard / input events ---
    document.addEventListener('input', function(e) {
        if (e.target.closest && e.target.closest('#__bgym_overlay')) return;
        var bid = findBid(e.target);
        if (!bid) return;
        // Debounce: wait 800ms of no typing before emitting a fill event
        if (window.__bgym_typing[bid]) {
            clearTimeout(window.__bgym_typing[bid].timeout_id);
        }
        var value = e.target.value !== undefined ? e.target.value : (e.target.textContent || '');
        window.__bgym_typing[bid] = {
            value: value,
            timeout_id: setTimeout(function() {
                window.__bgym_events.push({
                    type: 'fill',
                    bid: bid,
                    value: window.__bgym_typing[bid].value,
                    timestamp: Date.now()
                });
                delete window.__bgym_typing[bid];
            }, 800)
        };
    }, true);

    // Special keys (Enter, Escape, Tab, etc.)
    document.addEventListener('keydown', function(e) {
        if (e.target.closest && e.target.closest('#__bgym_overlay')) return;
        var specialKeys = [
            'Enter', 'Escape', 'Tab', 'Backspace', 'Delete',
            'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
            'Home', 'End', 'PageUp', 'PageDown',
            'F1','F2','F3','F4','F5','F6','F7','F8','F9','F10','F11','F12'
        ];
        // Also capture Ctrl/Cmd combos
        if (specialKeys.includes(e.key) || e.ctrlKey || e.metaKey) {
            // Don't capture bare modifier presses
            if (['Control', 'Meta', 'Shift', 'Alt'].includes(e.key)) return;

            var bid = findBid(e.target);
            var combo = '';
            if (e.ctrlKey || e.metaKey) combo += 'Control+';
            if (e.shiftKey) combo += 'Shift+';
            if (e.altKey) combo += 'Alt+';
            combo += e.key;

            window.__bgym_events.push({
                type: 'press',
                bid: bid,
                key: combo,
                timestamp: Date.now()
            });
        }
    }, true);

    // --- Scroll ---
    document.addEventListener('wheel', function(e) {
        if (e.target.closest && e.target.closest('#__bgym_overlay')) return;
        window.__bgym_events.push({
            type: 'scroll',
            x: e.clientX, y: e.clientY,
            deltaX: e.deltaX,
            deltaY: e.deltaY,
            bid: findBid(e.target),
            timestamp: Date.now()
        });
    }, true);

    // --- Select / dropdown ---
    document.addEventListener('change', function(e) {
        if (e.target.closest && e.target.closest('#__bgym_overlay')) return;
        if (e.target.tagName === 'SELECT') {
            var bid = findBid(e.target);
            if (!bid) return;
            var selected = Array.from(e.target.selectedOptions).map(function(o) { return o.value; });
            window.__bgym_events.push({
                type: 'select_option',
                bid: bid,
                value: selected,
                timestamp: Date.now()
            });
        }
    }, true);
})();
"""

def make_overlay_js(goal_text=""):
    """Build overlay JS with the task goal injected. Uses addEventListener instead of
    inline onclick to avoid CSP (Content Security Policy) blocking."""
    safe_goal = goal_text.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
    return """
(function() {
    if (document.getElementById('__bgym_overlay')) return;

    var overlay = document.createElement('div');
    overlay.id = '__bgym_overlay';
    overlay.style.cssText = 'position: fixed; top: 10px; right: 10px; z-index: 999999;';

    var panel = document.createElement('div');
    panel.id = '__bgym_panel';
    panel.style.cssText = 'background: #1a1a2e; color: #eee; border: 2px solid #16213e; border-radius: 8px; padding: 12px; width: 280px; font-family: monospace; font-size: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); max-height: 90vh; overflow-y: auto;';

    // Title bar (draggable handle)
    var title = document.createElement('div');
    title.style.cssText = 'font-weight: bold; margin-bottom: 8px; color: #e94560; cursor: grab; user-select: none; padding: 4px 0;';
    title.textContent = 'Trajectory Recorder (drag to move)';
    panel.appendChild(title);

    // --- Drag logic ---
    var isDragging = false;
    var dragOffsetX = 0, dragOffsetY = 0;
    title.addEventListener('mousedown', function(e) {
        isDragging = true;
        title.style.cursor = 'grabbing';
        var rect = overlay.getBoundingClientRect();
        dragOffsetX = e.clientX - rect.left;
        dragOffsetY = e.clientY - rect.top;
        e.preventDefault();
    });
    document.addEventListener('mousemove', function(e) {
        if (!isDragging) return;
        overlay.style.left = (e.clientX - dragOffsetX) + 'px';
        overlay.style.top = (e.clientY - dragOffsetY) + 'px';
        overlay.style.right = 'auto';
    });
    document.addEventListener('mouseup', function() {
        if (isDragging) {
            isDragging = false;
            title.style.cursor = 'grab';
        }
    });

    // Task goal
    var goalDiv = document.createElement('div');
    goalDiv.style.cssText = 'margin-bottom: 8px; padding: 8px; background: #16213e; border-radius: 4px; border-left: 3px solid #e94560; font-size: 11px; color: #ccc;';
    var goalLabel = document.createElement('div');
    goalLabel.style.cssText = 'font-weight: bold; color: #e94560; margin-bottom: 4px;';
    goalLabel.textContent = 'Task:';
    goalDiv.appendChild(goalLabel);
    var goalText = document.createElement('div');
    goalText.textContent = `""" + safe_goal + """`;
    goalDiv.appendChild(goalText);
    panel.appendChild(goalDiv);

    // Status
    var status = document.createElement('div');
    status.id = '__bgym_status';
    status.style.cssText = 'margin-bottom: 8px; font-size: 11px; color: #aaa;';
    status.textContent = 'Recording...';
    panel.appendChild(status);

    // Separator
    var hr1 = document.createElement('hr');
    hr1.style.cssText = 'border-color: #333; margin: 6px 0;';
    panel.appendChild(hr1);

    // Answer label
    var ansLabel = document.createElement('div');
    ansLabel.style.cssText = 'margin-bottom: 6px; font-size: 11px; font-weight: bold;';
    ansLabel.textContent = 'Send Answer:';
    panel.appendChild(ansLabel);

    // Answer textarea
    var textarea = document.createElement('textarea');
    textarea.id = '__bgym_answer';
    textarea.rows = 2;
    textarea.placeholder = 'Type your answer here...';
    textarea.style.cssText = 'width: 100%; background: #16213e; color: #eee; border: 1px solid #333; border-radius: 4px; padding: 4px; font-family: monospace; font-size: 11px; box-sizing: border-box;';
    panel.appendChild(textarea);

    // Submit button
    var submitBtn = document.createElement('button');
    submitBtn.textContent = 'Submit Answer';
    submitBtn.style.cssText = 'width: 100%; margin-top: 4px; padding: 6px; background: #0f3460; color: #eee; border: none; border-radius: 4px; cursor: pointer; font-family: monospace;';
    submitBtn.addEventListener('click', function() {
        var text = textarea.value;
        if (text.trim()) {
            window.__bgym_meta_actions.push({type: 'send_msg_to_user', text: text});
            textarea.value = '';
            status.textContent = 'Answer sent!';
        }
    });
    panel.appendChild(submitBtn);

    // Separator
    var hr2 = document.createElement('hr');
    hr2.style.cssText = 'border-color: #333; margin: 6px 0;';
    panel.appendChild(hr2);

    // Report Infeasible button
    var infeasBtn = document.createElement('button');
    infeasBtn.textContent = 'Report Infeasible';
    infeasBtn.style.cssText = 'width: 100%; padding: 6px; background: #533; color: #eee; border: none; border-radius: 4px; cursor: pointer; font-family: monospace;';
    infeasBtn.addEventListener('click', function() {
        var reason = prompt('Why is this task infeasible?');
        if (reason) {
            window.__bgym_meta_actions.push({type: 'report_infeasible', text: reason});
            status.textContent = 'Reported infeasible.';
        }
    });
    panel.appendChild(infeasBtn);

    // Stop button
    var stopBtn = document.createElement('button');
    stopBtn.textContent = 'Stop Recording';
    stopBtn.style.cssText = 'width: 100%; margin-top: 4px; padding: 6px; background: #e94560; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-family: monospace; font-weight: bold;';
    stopBtn.addEventListener('click', function() {
        window.__bgym_meta_actions.push({type: 'stop'});
        status.textContent = 'Stopping...';
    });
    panel.appendChild(stopBtn);

    overlay.appendChild(panel);
    document.body.appendChild(overlay);
})();
"""


# ---------------------------------------------------------------------------
# Coordinate -> BID mapping
# ---------------------------------------------------------------------------

def find_bid_at(x, y, extra_props):
    """Find the smallest clickable element whose bbox contains (x, y)."""
    best_bid = None
    best_area = float("inf")
    for bid, props in extra_props.items():
        bbox = props.get("bbox")
        if bbox is None:
            continue
        bx, by, bw, bh = bbox
        if bx <= x <= bx + bw and by <= y <= by + bh:
            area = bw * bh
            if area < best_area:
                best_area = area
                best_bid = bid
    return best_bid


# ---------------------------------------------------------------------------
# Event -> Action translation
# ---------------------------------------------------------------------------

def escape_str(s):
    """Escape a string for use in a Python action string."""
    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


def translate_events_to_actions(events, extra_props):
    """
    Translate a list of raw browser events into BrowserGym action strings.
    Returns a list of action strings.
    """
    actions = []
    # Accumulate scroll events
    scroll_accum = {"dx": 0, "dy": 0, "last_ts": 0}

    def flush_scroll():
        if scroll_accum["dx"] != 0 or scroll_accum["dy"] != 0:
            # Normalize scroll deltas to discrete steps
            dx = int(np.sign(scroll_accum["dx"]) * min(abs(scroll_accum["dx"]), 5))
            dy = int(np.sign(scroll_accum["dy"]) * min(abs(scroll_accum["dy"]), 5))
            actions.append(f"scroll({dx}, {dy})")
            scroll_accum["dx"] = 0
            scroll_accum["dy"] = 0

    for event in events:
        etype = event["type"]

        if etype == "click":
            flush_scroll()
            bid = event.get("bid")
            if not bid:
                bid = find_bid_at(event["x"], event["y"], extra_props)
            if bid:
                button = event.get("button", 0)
                if button == 2:
                    actions.append(f"click('{bid}', button='right')")
                elif button == 1:
                    actions.append(f"click('{bid}', button='middle')")
                else:
                    actions.append(f"click('{bid}')")

        elif etype == "dblclick":
            flush_scroll()
            bid = event.get("bid")
            if not bid:
                bid = find_bid_at(event["x"], event["y"], extra_props)
            if bid:
                actions.append(f"dblclick('{bid}')")

        elif etype == "fill":
            flush_scroll()
            bid = event.get("bid")
            if bid:
                value = escape_str(event.get("value", ""))
                actions.append(f"fill('{bid}', '{value}')")

        elif etype == "press":
            flush_scroll()
            bid = event.get("bid")
            key = event.get("key", "")
            # Detect back/forward navigation shortcuts
            if key in ("Alt+ArrowLeft", "Meta+[", "Control+["):
                actions.append("go_back()")
            elif key in ("Alt+ArrowRight", "Meta+]", "Control+]"):
                actions.append("go_forward()")
            elif bid:
                actions.append(f"press('{bid}', '{key}')")
            else:
                actions.append(f"keyboard_press('{key}')")

        elif etype == "scroll":
            # Accumulate scroll deltas within 300ms window
            ts = event.get("timestamp", 0)
            if ts - scroll_accum["last_ts"] > 300:
                flush_scroll()
            scroll_accum["dx"] += event.get("deltaX", 0)
            scroll_accum["dy"] += event.get("deltaY", 0)
            scroll_accum["last_ts"] = ts

        elif etype == "select_option":
            flush_scroll()
            bid = event.get("bid")
            if bid:
                values = event.get("value", [])
                actions.append(f"select_option('{bid}', {values})")

    flush_scroll()
    return actions


# ---------------------------------------------------------------------------
# Main recording loop
# ---------------------------------------------------------------------------

def inject_scripts(page, goal_text=""):
    """Inject event capture and overlay scripts into the page."""
    try:
        page.evaluate(EVENT_CAPTURE_JS)
        page.evaluate(make_overlay_js(goal_text))
    except Exception as e:
        print(f"[warn] Failed to inject scripts: {e}")


def poll_events(page):
    """Poll and flush captured events from the page."""
    try:
        events = page.evaluate("""
            (() => {
                var evts = window.__bgym_events || [];
                window.__bgym_events = [];
                return evts;
            })()
        """)
        return events
    except Exception:
        return []


def poll_meta_actions(page):
    """Poll meta-actions from the overlay panel."""
    try:
        actions = page.evaluate("""
            (() => {
                var acts = window.__bgym_meta_actions || [];
                window.__bgym_meta_actions = [];
                return acts;
            })()
        """)
        return actions
    except Exception:
        return []


def save_screenshot(obs, path):
    """Save screenshot from observation to a file."""
    screenshot = obs.get("screenshot")
    if screenshot is not None:
        img = Image.fromarray(screenshot)
        img.save(path)


def remark_elements(env):
    """Re-mark DOM elements with bid attributes so the JS event listeners can find them."""
    try:
        _pre_extract(env.page, tags_to_mark=env.tags_to_mark, lenient=True)
    except Exception as e:
        print(f"[warn] Failed to re-mark elements: {e}")


def record_step(env, raw_env, action, raw_event, step_idx, screenshots_dir, trajectory,
                goal_text="", is_meta=False):
    """
    Record a single step.

    For browser events (clicks, fills, etc.), the human already performed the action
    in the browser, so we skip re-execution and only extract observation/validation
    via raw_env.post_step().

    For meta-actions (send_msg_to_user, report_infeasible), we use env.step() since
    these need to be executed programmatically.
    """
    print(f"  step {step_idx + 1}: {action}")

    if is_meta:
        # Meta-actions need actual execution
        obs, reward, terminated, truncated, info = env.step(action)
        # For recording: always terminate after submitting an answer or reporting infeasible
        if "send_msg_to_user" in action or "report_infeasible" in action:
            terminated = True
    else:
        # Browser events already happened — skip re-execution, just observe
        raw_env.last_action = action
        raw_env.last_action_error = ""
        info = {
            "action_exec_start": time.time(),
            "action_exec_stop": time.time(),
            "action_exec_timeout": 0,
        }
        obs, reward, terminated, truncated, info = raw_env.post_step(info)

    save_screenshot(obs, screenshots_dir / f"step_{step_idx + 1:03d}.png")

    # Flatten AXTree to readable text
    axtree_txt = ""
    try:
        axtree_obj = obs.get("axtree_object")
        extra_props = obs.get("extra_element_properties", {})
        if axtree_obj:
            axtree_txt = flatten_axtree_to_str(axtree_obj, extra_properties=extra_props)
    except Exception as e:
        print(f"  [warn] Failed to flatten AXTree: {e}")

    trajectory["steps"].append({
        "step_idx": step_idx,
        "timestamp": time.time(),
        "action": action,
        "raw_event": raw_event,
        "url": obs.get("url", ""),
        "reward": float(reward),
        "action_error": obs.get("last_action_error", ""),
        "axtree": axtree_txt,
    })

    extra_props = obs.get("extra_element_properties", {})

    # Re-inject scripts and re-mark elements for next interaction
    inject_scripts(raw_env.page, goal_text)
    remark_elements(raw_env)

    return obs, reward, terminated, extra_props


def record(task_id, output_dir, max_steps=50, timeout=600):
    """Main recording function."""
    output_dir = Path(output_dir)
    task_name = f"webarena_pro.{task_id}"

    # Create output directory
    traj_dir = output_dir / f"{task_name}_{int(time.time())}"
    screenshots_dir = traj_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    print(f"[recorder] Starting task: {task_name}")
    print(f"[recorder] Output: {traj_dir}")

    # Create environment
    env = gym.make(
        f"browsergym/{task_name}",
        headless=False,
        wait_for_user_message=False,
    )

    obs, info = env.reset()

    # Unwrap to get the actual BrowserGym env with .page, .context, etc.
    raw_env = env.unwrapped

    goal = obs.get("goal", "")
    print(f"[recorder] Goal: {goal}")
    print(f"[recorder] URL: {obs.get('url', '')}")
    print()
    print("[recorder] Interact with the browser naturally.")
    print("[recorder] Use the overlay panel (top-right) to submit answers or stop.")
    print()

    # Inject scripts
    inject_scripts(raw_env.page, goal)
    # Mark elements so bids are visible to JS event listeners
    remark_elements(raw_env)

    trajectory = {
        "task_id": task_name,
        "goal": goal,
        "start_url": obs.get("url", ""),
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "steps": [],
    }

    # Save initial screenshot and AXTree
    save_screenshot(obs, screenshots_dir / "step_000.png")
    initial_axtree = ""
    try:
        axtree_obj = obs.get("axtree_object")
        extra_props = obs.get("extra_element_properties", {})
        if axtree_obj:
            initial_axtree = flatten_axtree_to_str(axtree_obj, extra_properties=extra_props)
    except Exception:
        pass
    trajectory["initial_axtree"] = initial_axtree

    step_idx = 0
    terminated = False
    start_time = time.time()
    last_extra_props = obs.get("extra_element_properties", {})

    try:
        while not terminated and step_idx < max_steps:
            # Check timeout
            if time.time() - start_time > timeout:
                print("[recorder] Timeout reached.")
                break

            # Poll events
            raw_events = poll_events(raw_env.page)
            meta_actions = poll_meta_actions(raw_env.page)

            # Handle meta-actions (these need actual execution via env.step)
            for ma in meta_actions:
                if ma["type"] == "stop":
                    print("[recorder] Stop requested by user.")
                    terminated = True
                    break
                elif ma["type"] == "send_msg_to_user":
                    action = f"send_msg_to_user('{escape_str(ma['text'])}')"
                    obs, reward, terminated, last_extra_props = record_step(
                        env, raw_env, action, ma, step_idx, screenshots_dir, trajectory,
                        goal_text=goal, is_meta=True,
                    )
                    step_idx += 1
                elif ma["type"] == "report_infeasible":
                    action = f"report_infeasible('{escape_str(ma['text'])}')"
                    obs, reward, terminated, last_extra_props = record_step(
                        env, raw_env, action, ma, step_idx, screenshots_dir, trajectory,
                        goal_text=goal, is_meta=True,
                    )
                    step_idx += 1

            if terminated:
                break

            # Translate browser events to BrowserGym actions
            # These are NOT re-executed — the human already performed them
            if raw_events:
                actions = translate_events_to_actions(raw_events, last_extra_props)
                for action in actions:
                    if step_idx >= max_steps:
                        break
                    try:
                        obs, reward, terminated, last_extra_props = record_step(
                            env, raw_env, action, raw_events, step_idx, screenshots_dir,
                            trajectory, goal_text=goal, is_meta=False,
                        )
                    except Exception as e:
                        print(f"  [warn] Failed to record step: {e}")
                        continue

                    step_idx += 1
                    if terminated:
                        break
            else:
                # No events, sleep briefly
                time.sleep(0.2)

    except KeyboardInterrupt:
        print("\n[recorder] Interrupted by user.")

    # Save trajectory
    trajectory["num_steps"] = step_idx
    trajectory["total_reward"] = sum(s.get("reward", 0) for s in trajectory["steps"])
    trajectory["terminated"] = terminated
    trajectory["end_time"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    traj_path = traj_dir / "trajectory.json"
    with open(traj_path, "w") as f:
        json.dump(trajectory, f, indent=2)

    print(f"\n[recorder] Saved trajectory to {traj_path}")
    print(f"[recorder] {step_idx} steps recorded, total reward: {trajectory['total_reward']}")

    env.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Record human trajectories for WebArena-Pro")
    parser.add_argument("--task_id", type=int, required=True, help="WebArena-Pro task ID (0-9)")
    parser.add_argument("--output_dir", type=str, default="./trajectories", help="Output directory")
    parser.add_argument("--max_steps", type=int, default=50, help="Max steps per trajectory")
    parser.add_argument("--timeout", type=int, default=600, help="Max recording time in seconds")
    args = parser.parse_args()

    record(
        task_id=args.task_id,
        output_dir=args.output_dir,
        max_steps=args.max_steps,
        timeout=args.timeout,
    )
