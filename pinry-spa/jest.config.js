module.exports = {
  testEnvironment: 'jsdom',
  resolver: '<rootDir>/tests/resolver.js',
  testMatch: ['**/tests/unit/**/*.spec.js'],
  moduleFileExtensions: ['js', 'json', 'vue'],
  transform: {
    '^.+\\.vue$': '@vue/vue3-jest',
    '^.+\\.m?js$': 'babel-jest',
    '.+\\.(png|jpe?g|gif|svg|webp)$': '<rootDir>/tests/fileTransformer.js',
  },
  moduleNameMapper: { '^@/(.*)$': '<rootDir>/src/$1' },
  transformIgnorePatterns: ['/node_modules/(?!.*(?:primevue|@primevue|@primeuix)/)'],
};
