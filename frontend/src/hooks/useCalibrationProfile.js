import { decodeCalibrationProfile, validateCalibrationProfile } from '../contracts/calibrationProfile.js'
import { useMessageStream } from './useMessageStream.js'

const PROFILE_CHECKER = {
  validate: validateCalibrationProfile,
  decode: (text) => {
    const { profile, ...rest } = decodeCalibrationProfile(text)
    return { ...rest, message: profile }
  },
}

// A profile is a finished result, not a stream, so it never goes stale.
export function useCalibrationProfile(source) {
  return useMessageStream(source, PROFILE_CHECKER, { staleAfterMs: Infinity })
}
