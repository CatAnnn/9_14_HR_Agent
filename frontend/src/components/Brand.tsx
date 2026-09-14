import { STATIC_ASSETS } from '../config/staticAssets';

interface BrandProps {
  small?: boolean;
}

export function Brand({ small = false }: BrandProps) {
  return (
    <div className={`brand-lockup ${small ? 'small' : ''}`}>
      <img src={STATIC_ASSETS.companyLogo} alt="Bosch" className="bosch-logo" />
    </div>
  );
}
