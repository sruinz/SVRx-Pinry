/* global __PINRY_BUILD_DEPENDENCIES__ */
export default function buildDependencies() {
  return typeof __PINRY_BUILD_DEPENDENCIES__ === 'undefined'
    ? { frontend: [], buildTools: [] } : __PINRY_BUILD_DEPENDENCIES__;
}
