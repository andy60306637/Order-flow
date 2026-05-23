/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts}'],
  theme: {
    extend: {
      colors: {
        bg:      '#131722',
        panel:   '#1e222d',
        border:  '#2a2e39',
        text:    '#d1d4dc',
        dim:     '#787b86',
        accent:  '#2962ff',
        up:      '#26a69a',
        down:    '#ef5350',
        yellow:  '#f0c040',
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', 'monospace'],
      },
    },
  },
  plugins: [],
}
