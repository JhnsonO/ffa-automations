# EXP7 real hardware result — run 31872659022

Date: 2026-08-15
Branch: `diag/zc-exp7-compile-proof`
Run: `31872659022`
Job: `94983577213`
Hardware: NVIDIA GeForce RTX 4090, driver 570.195.03, Vulkan 1.3.275.

## Source / harness gates

- Reco base: `e810a04ee29452b3cd6647cc98875033a2e0d1a0`.
- EXP7 payload: `ffa-automations@6cdbb96c9fd6a244860b9e1e8fb4b35ee4c0121a`.
- `vulkan.rs`: 1476 lines, SHA256 `4da792382d954f5ffe68865d5ae84db9e778c2f6e452917a7749149f50c41089`.
- `cuda.rs`: 1312 lines, SHA256 `e81e693071608caef213eb34e68335d064e3aded6c58214147f2b6be4ac303b7`.
- Payload hash gate: PASS.
- wgpu quartet pinned to `c8b6f2f00895210857f77f2a10fc1a32a80d5148`; resolution gate: PASS.
- EXP7 process exited 0 and `ZC_EXP7_COMPLETE` fired after two shared textures, before decode.
- Pod termination confirmed HTTP 204.

## Important host caveat

The generic preflight reported `PREFLIGHT_NVDEC=FAIL` (`cuvidGetDecoderCaps -> CUDA_ERROR_NO_DEVICE`) even though Vulkan, CUDA init and ORT CUDA passed. This does **not** invalidate EXP7: the experiment intentionally exited before decode and its load-bearing control directly exercised CUDA VMM allocation/export, CUDA writes/readback, Vulkan OPAQUE_FD import, Vulkan semaphore wait and Vulkan buffer readback. Do not use this run as NVDEC evidence.

## C — CUDA VMM -> Vulkan VkBuffer control

Capability query:

`ZC_EXP7_BUF_CAPS: ... EXPORTABLE=true IMPORTABLE=true DEDICATED_ONLY=false compatibleHandleTypes=OPAQUE_FD opaque_fd_in_compatible=true exportFromImportedHandleTypes=OPAQUE_FD`

Import details:

- requested CUDA allocation: 1,048,576 bytes
- CUDA VMM granularity-rounded allocation: 2,097,152 bytes
- Vulkan buffer memory requirement: 1,048,576 bytes, alignment 4
- Vulkan `allocationSize` used: 1,048,576 bytes (`memRequirements.size`), not the CUDA-rounded size
- memory type index: 1

Synchronized sentinel result at all three offsets (0, 524288, 1047552):

- CUDA DtoH: byte-exact = true
- Vulkan staging readback: byte-exact = true
- `ZC_EXP7_BUF_ALIAS_RESULT=PASS`

**Conclusion:** generic CUDA-VMM-export -> Vulkan OPAQUE_FD import aliases real memory correctly on this exact NVIDIA stack. The handle type/export direction is not generically broken.

## A/B — exact Vulkan image capability + dedicated-allocation queries

Y plane (`R8_UNORM`, LINEAR, TRANSFER_SRC|TRANSFER_DST|SAMPLED, 3840x2160):

- query result: SUCCESS
- EXPORTABLE=true
- IMPORTABLE=true
- DEDICATED_ONLY=false
- OPAQUE_FD compatible=true
- requiresDedicatedAllocation=false
- prefersDedicatedAllocation=false
- Vulkan memory requirement size=8,294,400, alignment=128, memoryTypeBits=0xf
- CUDA granularity-rounded allocation size=8,388,608

UV plane (`R8G8_UNORM`, same tiling/usage, 1920x1080):

- query result: SUCCESS
- EXPORTABLE=true
- IMPORTABLE=true
- DEDICATED_ONLY=false
- OPAQUE_FD compatible=true
- requiresDedicatedAllocation=false
- prefersDedicatedAllocation=false
- Vulkan memory requirement size=4,147,200, alignment=128, memoryTypeBits=0xf
- CUDA granularity-rounded allocation size=4,194,304

**Conclusion:** the exact image configurations are explicitly advertised as OPAQUE_FD-importable and neither requires nor prefers a dedicated allocation. Therefore the two previously-listed candidates "OPAQUE_FD/export path is generically incompatible" and "missing VkMemoryDedicatedAllocateInfo is required" are rejected by real evidence.

## Combined interpretation with Avenue 2

Avenue 2 run `31854089581` already showed the imported **image** does not observe a CUDA-written sentinel even though CUDA DtoH sees it byte-exact. EXP7 now shows the same CUDA VMM -> OPAQUE_FD mechanism works byte-exact when imported into a **VkBuffer**, while the exact image config is nominally supported by the driver.

This localizes the remaining failure to image-specific import/payload semantics or to a parameter that differs between the passing buffer import and failing image import. The strongest small next variable is `VkMemoryAllocateInfo::allocationSize`: the passing NVIDIA-style buffer control uses Vulkan `memRequirements.size`, while the current image path passes CUDA's larger granularity-rounded `shared_mem.alloc_size`.

## Next single-variable experiment (not yet run)

For the image import only, change **only** `VkMemoryAllocateInfo::allocation_size` from `shared_mem.alloc_size` to `mem_reqs.size`. Keep the same CUDA allocation/export, OPAQUE_FD type, memory type, image format/tiling/usage, layout transition, semaphore sync, wgpu state and Avenue-2 sentinel/readback. Re-run the synchronized image sentinel test.

Decision:
- If image sentinel becomes byte-exact: root cause is the imported image `allocationSize` mismatch; fix is narrowly localized.
- If image remains zero: allocationSize is ruled out and evidence strongly points to image-specific CUDA-VMM payload semantics. At that point the practical architecture candidate is shared VkBuffer-backed Y/UV planes plus a GPU-side buffer->texture copy (or Vulkan-owned image memory exported to CUDA), rather than further wgpu/render debugging.
