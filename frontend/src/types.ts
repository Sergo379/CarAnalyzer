export type BodyType =
  | "sedan"
  | "wagon"
  | "hatchback"
  | "liftback"
  | "coupe"
  | "convertible"
  | "suv"
  | "crossover"
  | "pickup"
  | "minivan"
  | "van";
export type BodyFilter = BodyType | "any";

export type Transmission = "any" | "automatic" | "manual" | "robot" | "cvt";
export type SearchRegion = "any" | "moscow" | "moscow_oblast" | "moscow_and_oblast";

export interface SearchForm {
  brand: string;
  model: string;
  year: number | "";
  body_type: BodyFilter | "";
  transmission: Transmission | "";
  region: SearchRegion;
  price: number | "";
  generation_id: string;
  modification_id: string;
}

export interface CarListing {
  source: string;
  external_id: string;
  brand: string;
  model: string;
  modification: string | null;
  generation: string | null;
  fuel_type: string | null;
  engine_displacement: number | null;
  power_hp: number | null;
  engine_code: string | null;
  drivetrain: string | null;
  year: number;
  body_type: BodyType;
  transmission: Transmission | null;
  price: number;
  url: string;
  location: string | null;
  city: string | null;
  region: string | null;
  checked_at: string;
  segment: string | null;
}

export interface ClassifiedListing {
  listing: CarListing;
  price_difference: number;
  price_difference_percent: number;
}

export interface ModelGroup {
  brand: string;
  model: string;
  listings_count: number;
  average_price: number;
  min_price: number;
  max_price: number;
}

export interface CategoryResult {
  listings: ClassifiedListing[];
  model_groups: ModelGroup[];
}

export interface CarProfile {
  brand: string;
  model: string;
  year: number;
  generation: string;
  segment: string | null;
  supported_body_types: BodyType[];
  technical_summary: string | null;
}

export interface ProblemProfile {
  common_problems: string[];
  problematic_components: string[];
  inspection_points: string[];
  expensive_failures: string[];
  risk_summary: string;
}

export interface SearchResult {
  source_vehicle: {
    brand: string;
    model: string;
    year: number;
    body_type: BodyFilter;
    transmission: Transmission;
    region: SearchRegion;
    price: number;
    modification: string | null;
    generation: string | null;
    segment: string | null;
  };
  source_status: Record<string, string>;
  source_details: Record<string, string>;
  source_listings: CategoryResult;
  source_model_group: ModelGroup | null;
  direct: CategoryResult;
  expensive: CategoryResult;
  cheaper: CategoryResult;
  car_knowledge: Record<string, {
    status: string;
    profile: CarProfile | null;
    problems: ProblemProfile | null;
  }>;
  warnings: string[];
}
