/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        ink: { 950: '#0b1020', 900: '#141a31', 800: '#1b2240', 700: '#232a4a' },
        accent: { DEFAULT: '#5b8cff', soft: 'rgba(91,140,255,0.15)' },
        good: { DEFAULT: '#25c26a', soft: 'rgba(37,194,106,0.15)' },
        warn: { DEFAULT: '#ffb04a', soft: 'rgba(255,176,74,0.15)' },
        bad:  { DEFAULT: '#ff6b6b', soft: 'rgba(255,107,107,0.15)' },
      },
    },
  },
  plugins: [],
}
