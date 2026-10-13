"use strict";

// Same numbers as face-data.json, inlined so a double-clicked index.html still draws.
window.FACE_DATA = {
  "blink": [
    0.55,
    0.12,
    0.08,
    0.35,
    0.8
  ],
  "screen": 466,
  "tick_ms": 50,
  "smooth_s": 0.16,
  "squint_arc": 0.6,
  "text_safe_r": 204,
  "robot_state_y": 100,
  "fields": [
    "eye_w",
    "eye_h",
    "eye_dy",
    "mouth_w",
    "mouth_h",
    "mouth_curve",
    "blush",
    "blink_ms",
    "brow",
    "brow_tilt",
    "brow_raise",
    "lid",
    "squint",
    "glow",
    "bob_hz",
    "bob_px"
  ],
  "pose_min": {
    "blink_ms": 1500.0,
    "blush": 0.0,
    "bob_hz": 0.15,
    "bob_px": 0.0,
    "brow": 0.0,
    "brow_raise": -4.0,
    "brow_tilt": -6.0,
    "eye_dy": -16.0,
    "eye_h": 4.0,
    "eye_w": 40.0,
    "glow": 0.2,
    "lid": 0.0,
    "mouth_curve": -8.0,
    "mouth_h": 0.0,
    "mouth_w": 0.0,
    "squint": 0.0
  },
  "pose_max": {
    "blink_ms": 9000.0,
    "blush": 1.0,
    "bob_hz": 2.0,
    "bob_px": 4.0,
    "brow": 1.0,
    "brow_raise": 8.0,
    "brow_tilt": 18.0,
    "eye_dy": 16.0,
    "eye_h": 96.0,
    "eye_w": 72.0,
    "glow": 1.0,
    "lid": 0.6,
    "mouth_curve": 14.0,
    "mouth_h": 24.0,
    "mouth_w": 90.0,
    "squint": 1.0
  },
  "poses": [
    {
      "blink_ms": 4500.0,
      "blush": 0.0,
      "bob_hz": 0.5,
      "bob_px": 1.5,
      "brow": 0.0,
      "brow_raise": 0.0,
      "brow_tilt": 0.0,
      "eye_dy": 0.0,
      "eye_h": 78.0,
      "eye_w": 58.0,
      "glow": 0.6,
      "lid": 0.0,
      "mouth_curve": 0.0,
      "mouth_h": 4.0,
      "mouth_w": 28.0,
      "squint": 0.0
    },
    {
      "blink_ms": 3200.0,
      "blush": 0.9,
      "bob_hz": 1.4,
      "bob_px": 2.5,
      "brow": 0.7,
      "brow_raise": 6.0,
      "brow_tilt": 0.0,
      "eye_dy": -4.0,
      "eye_h": 86.0,
      "eye_w": 66.0,
      "glow": 1.0,
      "lid": 0.0,
      "mouth_curve": 12.0,
      "mouth_h": 6.0,
      "mouth_w": 44.0,
      "squint": 1.0
    },
    {
      "blink_ms": 7000.0,
      "blush": 0.55,
      "bob_hz": 0.25,
      "bob_px": 1.0,
      "brow": 0.0,
      "brow_raise": -1.0,
      "brow_tilt": 0.0,
      "eye_dy": 4.0,
      "eye_h": 56.0,
      "eye_w": 62.0,
      "glow": 0.4,
      "lid": 0.4,
      "mouth_curve": 7.0,
      "mouth_h": 4.0,
      "mouth_w": 34.0,
      "squint": 0.2
    },
    {
      "blink_ms": 6500.0,
      "blush": 0.1,
      "bob_hz": 0.3,
      "bob_px": 1.0,
      "brow": 1.0,
      "brow_raise": 1.0,
      "brow_tilt": 16.0,
      "eye_dy": 8.0,
      "eye_h": 60.0,
      "eye_w": 52.0,
      "glow": 0.3,
      "lid": 0.3,
      "mouth_curve": -6.0,
      "mouth_h": 4.0,
      "mouth_w": 24.0,
      "squint": 0.0
    },
    {
      "blink_ms": 2400.0,
      "blush": 0.0,
      "bob_hz": 1.2,
      "bob_px": 2.0,
      "brow": 1.0,
      "brow_raise": 5.0,
      "brow_tilt": 8.0,
      "eye_dy": -6.0,
      "eye_h": 92.0,
      "eye_w": 54.0,
      "glow": 0.9,
      "lid": 0.0,
      "mouth_curve": -3.0,
      "mouth_h": 6.0,
      "mouth_w": 20.0,
      "squint": 0.0
    }
  ],
  "mood_names": {
    "alert": [
      -0.45,
      0.75
    ],
    "idle": [
      0.0,
      0.0
    ],
    "listening": [
      0.2,
      0.45
    ],
    "message": [
      0.35,
      0.25
    ],
    "offline": [
      -0.25,
      -0.6
    ],
    "sleep": [
      0.1,
      -1.0
    ],
    "speaking": [
      0.25,
      0.35
    ],
    "thinking": [
      0.0,
      0.25
    ],
    "talking": [
      0.25,
      0.35
    ],
    "talking_happy": [
      0.6,
      0.45
    ],
    "talking_sad": [
      -0.4,
      -0.05
    ]
  },
  "styles": {
    "alert": {
      "label": "Alert",
      "mood": "alert",
      "fixed": false,
      "glow": true,
      "eye": 16757310,
      "body": 14236218,
      "mouth": 8017184,
      "accent": 16757310,
      "eye_dx": 0,
      "eye_dy": 0,
      "eye_w": 0,
      "eye_h": 0,
      "mouth_w": 0,
      "mouth_h": 0
    },
    "idle": {
      "label": "Ready",
      "mood": "idle",
      "fixed": false,
      "glow": true,
      "eye": 3921407,
      "body": 12033248,
      "mouth": 1793648,
      "accent": 3921407,
      "eye_dx": 0,
      "eye_dy": 0,
      "eye_w": 0,
      "eye_h": 0,
      "mouth_w": 0,
      "mouth_h": 0
    },
    "listening": {
      "label": "Listening",
      "mood": "listening",
      "fixed": false,
      "glow": true,
      "eye": 10220543,
      "body": 4176047,
      "mouth": 2788259,
      "accent": 10220543,
      "eye_dx": 0,
      "eye_dy": 0,
      "eye_w": 0,
      "eye_h": 0,
      "mouth_w": 0,
      "mouth_h": 0
    },
    "message": {
      "label": "Message",
      "mood": "message",
      "fixed": false,
      "glow": true,
      "eye": 15235577,
      "body": 12033248,
      "mouth": 7024499,
      "accent": 15235577,
      "eye_dx": 0,
      "eye_dy": -4,
      "eye_w": 0,
      "eye_h": 0,
      "mouth_w": 0,
      "mouth_h": 0
    },
    "offline": {
      "label": "Offline",
      "mood": "offline",
      "fixed": true,
      "glow": false,
      "eye": 3818064,
      "body": 10261680,
      "mouth": 2501685,
      "accent": 3818064,
      "eye_dx": 0,
      "eye_dy": 6,
      "eye_w": 58,
      "eye_h": 10,
      "mouth_w": 28,
      "mouth_h": 4
    },
    "sleep": {
      "label": "",
      "mood": "sleep",
      "fixed": true,
      "glow": false,
      "eye": 2372160,
      "body": 8022216,
      "mouth": 0,
      "accent": 4475991,
      "eye_dx": 0,
      "eye_dy": 10,
      "eye_w": 56,
      "eye_h": 6,
      "mouth_w": 0,
      "mouth_h": 0
    },
    "speaking": {
      "label": "Speaking",
      "mood": "speaking",
      "fixed": false,
      "glow": true,
      "eye": 6287552,
      "body": 15761006,
      "mouth": 6287552,
      "accent": 6287552,
      "eye_dx": 0,
      "eye_dy": 0,
      "eye_w": 0,
      "eye_h": 0,
      "mouth_w": 0,
      "mouth_h": 0
    },
    "thinking": {
      "label": "Thinking",
      "mood": "thinking",
      "fixed": true,
      "glow": true,
      "eye": 11111423,
      "body": 15253834,
      "mouth": 4865146,
      "accent": 11111423,
      "eye_dx": 12,
      "eye_dy": -14,
      "eye_w": 62,
      "eye_h": 36,
      "mouth_w": 22,
      "mouth_h": 4
    }
  },
  "pal": {
    "kGray": 3818064,
    "kTrack": 1317151,
    "kBlue": 5088255,
    "kMint": 6287552,
    "kVisorTop": 1251360,
    "kVisorBot": 329482,
    "kVisorEdge": 2304822,
    "kBlush": 16748472,
    "kTextDim": 6187383
  },
  "geo": {
    "kBlushH": 16,
    "kBlushOpaMax": 150,
    "kBlushW": 36,
    "kBlushX": 92,
    "kBlushY": 34,
    "kMouthY": 50,
    "kSmileBoxH": 48,
    "kSmileBoxW": 120,
    "kVisorH": 172,
    "kVisorW": 300,
    "kVisorY": -14
  },
  "layout": {
    "kRbArcA0": 205,
    "kRbArcA1": 335,
    "kRbArcW": 9,
    "kRbBrowGap": 12,
    "kRbBrowLen": 30,
    "kRbBrowW": 5,
    "kRbEyeGap": 62,
    "kRbEyeY": -8
  }
};
