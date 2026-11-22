// Avatar proxy (handoff Phase 2, T2.3): a box at the avatar's footprint and height, with a visible
// marker at the footprint centre — the skid-steer pivot — so the pivot can be seen while debugging.
// The rover asset replaces this in Phase 4; the pose contract stays the same.

import { BoxGeometry, CylinderGeometry, Group, Mesh, MeshStandardMaterial } from 'three'
import { AVATAR_HEIGHT_M, AVATAR_LENGTH_M, AVATAR_WIDTH_M } from '../sim/constants.js'
import { applyPose } from './frame.js'

const MARKER_PROUD_M = 0.3

export function createAvatarProxy() {
  const group = new Group()
  group.name = 'avatar'
  const bodyMat = new MeshStandardMaterial({ color: 0x5a6270, roughness: 0.7, metalness: 0 })
  const markMat = new MeshStandardMaterial({ color: 0xc8501e, roughness: 0.6, metalness: 0 })

  // Local frame: +X forward (length), +Y up, ±Z across (width). Footprint centre at the origin.
  const body = new Mesh(new BoxGeometry(AVATAR_LENGTH_M, AVATAR_HEIGHT_M, AVATAR_WIDTH_M), bodyMat)
  body.position.y = AVATAR_HEIGHT_M / 2
  body.name = 'proxy_body'
  group.add(body)

  // Pivot marker: a post through the footprint centre, standing proud of the top.
  const postH = AVATAR_HEIGHT_M + MARKER_PROUD_M
  const post = new Mesh(new CylinderGeometry(0.02, 0.02, postH, 12), markMat)
  post.position.y = postH / 2
  post.name = 'pivot_marker'
  group.add(post)

  // Heading tab on the front face, so forward is unambiguous from any angle.
  const nose = new Mesh(new BoxGeometry(0.04, 0.12, AVATAR_WIDTH_M * 0.5), markMat)
  nose.position.set(AVATAR_LENGTH_M / 2 + 0.02, AVATAR_HEIGHT_M * 0.75, 0)
  nose.name = 'heading_tab'
  group.add(nose)

  return {
    object: group,
    setPose: (pose) => applyPose(group, pose),
    dispose: () => {
      group.traverse((o) => o.geometry?.dispose())
      bodyMat.dispose()
      markMat.dispose()
    },
  }
}
