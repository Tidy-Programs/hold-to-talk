/**
 * Hold to talk's mark (three sound bars on a teal disc) and its name. It
 * replaces Handy's wordmark; the component keeps its upstream name so the
 * places that use it stay unchanged.
 */
// The product name is a brand, so it is not translated.
const NAME_LINES = ["Hold to", "talk"] as const;

const HandyTextLogo = ({
  width,
  height,
  className,
}: {
  width?: number;
  height?: number;
  className?: string;
}) => {
  return (
    <svg
      width={width}
      height={height}
      className={className}
      viewBox="0 0 930 328"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      role="img"
      aria-label="Hold to talk"
    >
      <circle cx="164" cy="164" r="160" fill="#137a70" />
      <rect x="82" y="111" width="44" height="106" rx="22" fill="#fff" />
      <rect x="142" y="61" width="44" height="206" rx="22" fill="#fff" />
      <rect x="202" y="92" width="44" height="144" rx="22" fill="#fff" />
      <text
        x="360"
        y="150"
        fill="currentColor"
        fontFamily="system-ui, -apple-system, 'Segoe UI', sans-serif"
        fontSize="128"
        fontWeight="700"
      >
        {NAME_LINES[0]}
      </text>
      <text
        x="360"
        y="290"
        fill="currentColor"
        fontFamily="system-ui, -apple-system, 'Segoe UI', sans-serif"
        fontSize="128"
        fontWeight="700"
      >
        {NAME_LINES[1]}
      </text>
    </svg>
  );
};

export default HandyTextLogo;
