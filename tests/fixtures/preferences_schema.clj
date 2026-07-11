(def default-schema
  {:type :object
   :scope :global
   :properties
   {:code {:type :object
           :scope :project
           :properties
           {:font-size {:type :number :default 12 :ui {:step 1}}
            :theme {:type :enum :values [:light :dark] :default :dark}}}
    :enabled {:type :boolean :default true}}})
