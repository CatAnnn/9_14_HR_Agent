# Feedback and assessment stock-photo provenance · 2026-09

This record covers the six real-photography assets used by the feedback-and-assessment solution family. No generated imagery is used in these files.

## License and processing

- Provider: Pexels.
- License: https://www.pexels.com/license/
- Verification date: 2026-09-05.
- Each source page was checked for the Pexels “Free to use” status and photographer attribution.
- The Pexels license permits website use and image modification. It does not permit implying that a pictured person or brand endorses the product; the images are used here only as neutral workplace illustrations.
- Originals were downloaded from `images.pexels.com` at their full available resolution.
- Delivery format: lossy WebP (VP8), 1920 × 1080, `yuv420p`, one frame.
- Processing: preserve aspect ratio, scale to cover 1920 × 1080, then take a centered 16:9 crop; encode with `libwebp`, quality 90, compression level 6, preset `picture`.

Processing command:

```bash
ffmpeg -hide_banner -loglevel error -y \
  -i '<original.jpeg>' \
  -vf 'scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1' \
  -c:v libwebp -quality 90 -compression_level 6 -preset picture \
  -frames:v 1 \
  '<final.webp>'
```

## Feedback category

- Concept: multiple workplace perspectives seen naturally through architectural glass rather than a staged review meeting.
- Pexels title: “People behind Reflection in Window”.
- Photographer: Bayram Yalçın.
- Source page: https://www.pexels.com/photo/people-behind-reflection-in-window-15228389/
- License page: https://www.pexels.com/license/
- Original image URL: https://images.pexels.com/photos/15228389/pexels-photo-15228389.jpeg
- Original dimensions: 3259 × 4887 JPEG.
- Original SHA-256: `347a9bd9949b56e8b3ae40b843ba5f4127022c664d1e610f34174c01eaae899e`.
- Final path: `data/frontend/assets/landing/solution-category-feedback-premium-202609.webp`.
- Final dimensions and size: 1920 × 1080 WebP, 464,574 bytes.
- Final SHA-256: `7fe0e66ea0ec440d5eb87d2ba54c4c12233dfd74bd337346bbdea891b575dab5`.
- Crop notes: centered portrait-to-landscape crop; the full original width is retained and approximately 1,527 pixels are removed from both the top and bottom. The crop preserves three layered figures, the glass reflection, and the warm architectural texture.
- Recommended Chinese alt: `玻璃倒影中的专业人员在开放办公空间协作`.
- Recommended narrow-screen focal point: `50% 50%`.

## 360-degree feedback

- Concept: several colleagues contribute visibly different viewpoints around one shared discussion.
- Pexels title: “Colleagues Having a Discussion”.
- Photographer: Tiger Lily.
- Source page: https://www.pexels.com/photo/colleagues-having-a-discussion-7108757/
- License page: https://www.pexels.com/license/
- Original image URL: https://images.pexels.com/photos/7108757/pexels-photo-7108757.jpeg
- Original dimensions: 5260 × 3507 JPEG.
- Original SHA-256: `b16d05bb551ba14c8ede4848685179745f3f9e6ded83a1f39657ee9219c6009a`.
- Final path: `data/frontend/assets/landing/method-360-degree-feedback-premium-202609.webp`.
- Final dimensions and size: 1920 × 1080 WebP, 211,470 bytes.
- Final SHA-256: `742af6ff01423c6014a3c3742b138dda77ec28d7bd688d62738a3500bc6f360b`.
- Crop notes: centered horizontal crop removes about 274 original pixels from the top and bottom. All four principal participants, the active speaker, and the listening reactions remain visible.
- Recommended Chinese alt: `多位同事围绕同一议题交换不同视角`.
- Recommended narrow-screen focal point: `40% 50%`.

## Leadership self-assessment

- Concept: a leader pauses alone beside a high-rise window, creating a restrained editorial image of self-reflection rather than another team scene.
- Pexels title: “Woman In A Blazer Standing Beside Window”.
- Photographer: Tima Miroshnichenko.
- Source page: https://www.pexels.com/photo/woman-in-a-blazer-standing-beside-window-5717631/
- License page: https://www.pexels.com/license/
- Original image URL: https://images.pexels.com/photos/5717631/pexels-photo-5717631.jpeg
- Original dimensions: 3828 × 5742 JPEG.
- Original SHA-256: `1a4293ab1215f90318e2d5b4bf9766c754e7b2fe4d20b9f5809b8a703bc1b5be`.
- Final path: `data/frontend/assets/landing/method-leadership-self-assessment-premium-202609.webp`.
- Final dimensions and size: 1920 × 1080 WebP, 557,360 bytes.
- Final SHA-256: `223951a56e02863d95491e61deac1e2598ea9ba4fd9a1ba77182468ed3c0101b`.
- Crop notes: centered portrait-to-landscape crop retains the window structure, the leader's complete upper-body posture, and substantial quiet architectural space; approximately 1,794 original pixels are removed from both the top and bottom.
- Recommended Chinese alt: `管理者独自在落地窗前沉思`.
- Recommended narrow-screen focal point: `52% 50%`.

## Belbin team roles

- Concept: complementary roles are shown in a real automotive engineering workspace, with people and equipment sharing the frame instead of posing around a conference table.
- Pexels title: “Engineers in Workshop”.
- Photographer: ThisIsEngineering.
- Source page: https://www.pexels.com/photo/engineers-in-workshop-3862129/
- License page: https://www.pexels.com/license/
- Original image URL: https://images.pexels.com/photos/3862129/pexels-photo-3862129.jpeg
- Original dimensions: 5304 × 7952 JPEG.
- Original SHA-256: `6142150e9d0d33fe4a31a327b6f9806f2222317285ed38d180c8b8a88d1bbf58`.
- Final path: `data/frontend/assets/landing/method-belbin-team-roles-premium-202609.webp`.
- Final dimensions and size: 1920 × 1080 WebP, 389,816 bytes.
- Final SHA-256: `ea9727dcac5798286bfae2220ad4534367778112a477a7af76313017971f6ac6`.
- Crop notes: centered portrait-to-landscape crop keeps the full-width engineering rig and the three-person collaboration on the right; approximately 2,484 original pixels are removed from both the top and bottom. The unused upper wall becomes calm copy space without fabricating pixels.
- Recommended Chinese alt: `三名工程师在汽车原型设备旁协作分析`.
- Recommended narrow-screen focal point: `73% 50%`.

## Soft-skills assessment

- Concept: attentive listening is the primary visible behavior in a focused one-to-one workplace conversation.
- Pexels title: “Serious woman listening to colleague in office”.
- Photographer: Anna Shvets.
- Source page: https://www.pexels.com/photo/serious-woman-listening-to-colleague-in-office-5324913/
- License page: https://www.pexels.com/license/
- Original image URL: https://images.pexels.com/photos/5324913/pexels-photo-5324913.jpeg
- Original dimensions: 6240 × 4160 JPEG.
- Original SHA-256: `396fc0ff96baa90d4a9c6700541cf839fe25e10790b7c174e6ee309d334e7bf9`.
- Final path: `data/frontend/assets/landing/method-soft-skills-assessment-premium-202609.webp`.
- Final dimensions and size: 1920 × 1080 WebP, 416,304 bytes.
- Final SHA-256: `add7890f06fd06b852a945e8ba602b930462206d225fa674ec7796dbe3fee0d3`.
- Crop notes: centered horizontal crop removes 325 original pixels from the top and bottom. The listener's expression and the colleague's natural foreground gesture remain together, while the environment stays recognizably professional.
- Recommended Chinese alt: `一名员工专注倾听同事表达观点`.
- Recommended narrow-screen focal point: `52% 50%`.

## Workplace-skills assessment

- Concept: observable technical capability is represented by two engineers inspecting and adjusting a real circuit-board assembly at close range.
- Pexels title: “Female Engineer Working in Workshop”.
- Photographer: ThisIsEngineering.
- Source page: https://www.pexels.com/photo/female-engineer-working-in-workshop-3912476/
- License page: https://www.pexels.com/license/
- Original image URL: https://images.pexels.com/photos/3912476/pexels-photo-3912476.jpeg
- Original dimensions: 7952 × 5304 JPEG.
- Original SHA-256: `bad5aeffda65efd437c45ea889caa9ba7b6bfcbb24104d28642fb833475a5d64`.
- Final path: `data/frontend/assets/landing/method-workplace-skills-assessment-premium-202609.webp`.
- Final dimensions and size: 1920 × 1080 WebP, 247,816 bytes.
- Final SHA-256: `e0358c3752214cdaee2eabe61447f1bcc6688fe36e0da25fad708a51d19a9b3a`.
- Crop notes: centered horizontal crop removes approximately 415 original pixels from the top and bottom. Both engineers' expressions, the hands-on task, safety glasses, and the circuit-board evidence remain visible.
- Recommended Chinese alt: `两名工程师近距离检查并调整电路板`.
- Recommended narrow-screen focal point: `85% 50%`.

## Visual verification

- All six final files were opened after encoding and checked at their native 1920 × 1080 dimensions.
- The set deliberately alternates architectural reflection, a multi-person feedback exchange, solitary reflection, a wide engineering system, attentive interpersonal listening, and a close technical task.
- No generated-image source is used in any of the six final files.
- No stretching, synthetic background extension, generative fill, compositing, or subject removal was applied; only deterministic scale, crop, and WebP encoding were used.
- The original source pages, creator identities, original download URLs, dimensions, and content hashes are recorded above for auditability.
